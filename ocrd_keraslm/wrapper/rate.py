from __future__ import absolute_import
from math import log
from bisect import insort_left
from numpy.linalg import norm

from ocrd import Processor, MIMETYPE_PAGE
from ocrd.utils import getLogger, concat_padded, xywh_from_points, points_from_xywh
from ocrd.model.ocrd_page import from_file, to_xml, GlyphType, CoordsType, TextEquivType
from ocrd.model.ocrd_page_generateds import MetadataItemType, LabelsType, LabelType

from ocrd_keraslm.wrapper.config import OCRD_TOOL
from ocrd_keraslm import lib

logger = getLogger('processor.KerasRate')

CHOICE_THRESHOLD_NUM = 4 # maximum number of choices to try per element
CHOICE_THRESHOLD_CONF = 0.1 # maximum score drop from best choice to try per element
#beam_width = 100 # maximum number of best partial paths to consider during search with alternative_decoding
BEAM_CLUSTERING_ENABLE = True # enable pruning partial paths by history clustering
BEAM_CLUSTERING_DIST = 5 # maximum distance between state vectors to form a cluster
MAX_ELEMENTS = 500 # maximum number of lower level elements embedded within each element (for word/glyph iterators)

class KerasRate(Processor):
    
    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools']['ocrd-keraslm-rate']
        kwargs['version'] = OCRD_TOOL['version']
        super(KerasRate, self).__init__(*args, **kwargs)
        if not hasattr(self, 'workspace') or not self.workspace: # no parameter/workspace for --dump-json or --version (no processing)
            return
        
        self.rater = lib.Rater()
        self.rater.load_config(self.parameter['config_file'])
        if self.rater.stateful: # override necessary before compilation:
            self.rater.length = 1 # allow single-sample batches
            self.rater.minibatch_size = self.rater.length # make sure states are consistent with windows after 1 minibatch
        if self.parameter['alternative_decoding']:
            # override:
            self.rater.stateful = False # no implicit state transfer
            self.rater.incremental = True # but explicit state transfer
        self.rater.configure()
        self.rater.load_weights(self.parameter['weight_file'])
    
    def process(self):
        """
        Performs the rating.
        """
        level = self.parameter['textequiv_level']
        beam_width = self.parameter['beam_width']
        for (n, input_file) in enumerate(self.input_files):
            logger.info("INPUT FILE %i / %s", n, input_file)
            pcgts = from_file(self.workspace.download_file(input_file))
            logger.info("Scoring text in page '%s' at the %s level", pcgts.get_pcGtsId(), level)
            metadata = pcgts.get_Metadata() # ensured by from_file()
            metadata.add_MetadataItem(
                MetadataItemType(type_="processingStep",
                                 name=OCRD_TOOL['tools']['ocrd-keraslm-rate']['steps'][0],
                                 value='ocrd-keraslm-rate',
                                 Labels=[LabelsType(externalRef="parameters",
                                                    Label=[LabelType(type_=name,
                                                                     value=self.parameter[name])
                                                           for name in self.parameter.keys()])]))
            text = []
            # white space at word boundaries, newline at line/region boundaries: required for LM input
            # FIXME: tokenization ambiguity (i.e. segmenting punctuation into Word suffixes or extra elements)
            #        is handled very differently between GT and ocrd_tesserocr annotation...
            #        we can only reproduce segmentation if Word boundaries exactly coincide with white space!
            regions = pcgts.get_Page().get_TextRegion()
            if not regions:
                logger.warning("Page contains no text regions")
            first_region = True
            for region in regions:
                if level == 'region':
                    logger.debug("Getting text in region '%s'", region.id)
                    if not first_region:
                        text.append((None, [TextEquivType(Unicode=u'\n')])) # LM output will not appear in annotation (conf cannot be combined to accurate perplexity from output)
                    first_region = False
                    textequivs = region.get_TextEquiv()
                    if textequivs:
                        text.append((region, filter_choices(textequivs)))
                    else:
                        logger.warning("Region '%s' contains no text results", region.id)
                    continue
                lines = region.get_TextLine()
                if not lines:
                    logger.warning("Region '%s' contains no text lines", region.id)
                first_line = True
                for line in lines:
                    if level == 'line':
                        logger.debug("Getting text in line '%s'", line.id)
                        if not first_line or not first_region:
                            text.append((None, [TextEquivType(Unicode=u'\n')])) # LM output will not appear in annotation (conf cannot be combined to accurate perplexity from output)
                        first_line = False
                        textequivs = line.get_TextEquiv()
                        if textequivs:
                            text.append((line, filter_choices(textequivs)))
                        else:
                            logger.warning("Line '%s' contains no text results", line.id)
                        continue
                    words = line.get_Word()
                    if not words:
                        logger.warning("Line '%s' contains no words", line.id)
                    first_word = True
                    for word in words:
                        if level == 'word':
                            logger.debug("Getting text in word '%s'", word.id)
                            if not first_word:
                                text.append((None, [TextEquivType(Unicode=u' ')])) # LM output will not appear in annotation (conf cannot be combined to accurate perplexity from output)
                            elif not first_line or not first_region:
                                text.append((None, [TextEquivType(Unicode=u'\n')])) # LM output will not appear in annotation (conf cannot be combined to accurate perplexity from output)
                            first_word = False
                            textequivs = word.get_TextEquiv()
                            if textequivs:
                                text.append((word, filter_choices(textequivs)))
                            else:
                                logger.warning("Word '%s' contains no text results", word.id)
                            continue
                        space_char = None
                        if not first_word:
                            space_char = u' '
                        elif not first_line or not first_region:
                            space_char = u'\n'
                        if space_char: # space required for LM input
                            space_textequiv = TextEquivType(Unicode=space_char)
                            if self.parameter['add_space_glyphs']:
                                xywh = xywh_from_points(word.get_Coords().points)
                                xywh['w'] = 0
                                xywh['h'] = 0
                                space_glyph = GlyphType(id='%s_space' % word.id,
                                                        TextEquiv=[space_textequiv],
                                                        Coords=CoordsType(points_from_xywh(xywh))) # empty box
                                word.insert_Glyph_at(0, space_glyph) # add a pseudo glyph in annotation
                            else:
                                text.append((None, [space_textequiv])) # LM output will not appear in annotation (conf cannot be combined to accurate perplexity from output)
                        glyphs = word.get_Glyph()
                        if not glyphs:
                            logger.warning("Word '%s' contains no glyphs", word.id)
                        for glyph in glyphs:
                            logger.debug("Getting text in glyph '%s'", glyph.id)
                            textequivs = glyph.get_TextEquiv()
                            if textequivs:
                                text.append((glyph, filter_choices(textequivs)))
                            else:
                                logger.warning("Glyph '%s' contains no text results", glyph.id)
                        first_word = False
                    first_line = False
                first_region = False
            if self.parameter['alternative_decoding']:
                logger.info("Rating %d elements including its alternatives", len(text))
                # initial state; todo: pass from previous page
                next_fringe = [lib.Node(parent=None, state=None, value=b'\n'[0], cost=0.0, extras=None)]
                for element in text: # tuple of reference and list of its alternative textequivs
                    logger.debug("Rating '%s', combining %d new inputs with %d existing paths", element[0].id if element[0] else "space", len(element[1]), len(next_fringe))
                    fringe = next_fringe
                    next_fringe = []
                    for node in fringe:
                        new_nodes = [lib.Node(parent=node, state=node.state, value=node.value, cost=0.0, extras=(element[0], textequiv)) for textequiv in element[1]] # copies of node (keeping value+state until prediction)
                        alternatives = [textequiv.Unicode.encode("utf-8") for textequiv in element[1]] # byte sequences
                        for i in range(MAX_ELEMENTS): # accumulate states and costs of all alternatives (of different length) in node
                            updates = [j for j in range(len(element[1])) if i < len(alternatives[j])] # indices to update
                            if updates == []:
                                break
                            preds, states = self.rater.rate_single([new_nodes[u].value for u in updates], [new_nodes[u].state for u in updates])
                            for j, (new_node, alternative) in enumerate([(new_nodes[u], alternatives[u]) for u in updates]):
                                new_node.value = alternative[i]
                                new_node.state = states[j]
                                new_node.cum_cost += -log(max(preds[j][new_node.value], 1e-99), 2)
                        for new_node in new_nodes:
                            def history_clustering(next_fringe):
                                for old_node in next_fringe:
                                    if (new_node.value == old_node.value and
                                        all(norm(new_node.state[layer]-old_node.state[layer]) < BEAM_CLUSTERING_DIST for layer in range(self.rater.depth))):
                                        if old_node.cum_cost < new_node.cum_cost:
                                            # logger.debug("discarding %s in favour of %s due to history clustering",
                                            #              ''.join([prev_node.extras[1].Unicode for prev_node in new_node.to_sequence()[1:]]),
                                            #              ''.join([prev_node.extras[1].Unicode for prev_node in old_node.to_sequence()[1:]]))
                                            return True # continue with next new_node
                                        else:
                                            # logger.debug("neglecting %s in favour of %s due to history clustering",
                                            #              ''.join([prev_node.extras[1].Unicode for prev_node in old_node.to_sequence()[1:]]),
                                            #              ''.join([prev_node.extras[1].Unicode for prev_node in new_node.to_sequence()[1:]]))
                                            next_fringe.remove(old_node)
                                            break # immediately proceed to insert new_node
                                return False # proceed to insert new_node (no clustering possible)
                            if BEAM_CLUSTERING_ENABLE and history_clustering(next_fringe):
                                continue
                            insort_left(next_fringe, new_node) # insert sorted by cumulative costs
                            # todo: incorporate input confidences, too (weighted product or as input into LM)
                    # todo: history clustering for pruning paths by joining similar nodes (instead of neglecting costly nodes)
                    #logger.debug("Shrinking %d paths to best %d", len(next_fringe), beam_width)
                    next_fringe = next_fringe[:beam_width] # keep best paths (equals batch size)
                best = next_fringe[0] # best-scoring path
                best_len = 0
                for node in best.to_sequence()[1:]: # ignore root node
                    element = node.extras[0]
                    if element: # not just space
                        textequivs = element.get_TextEquiv()
                        textequiv = node.extras[1]
                        element.set_TextEquiv([textequiv]) # delete others
                        textequiv_len = len(textequiv.Unicode.encode("utf-8"))
                        best_len += textequiv_len
                        textequiv.set_conf(pow(2.0, -(node.cum_cost-node.parent.cum_cost)/textequiv_len)) # average probability
                        #print(textequiv.Unicode, end='')
                    else:
                        best_len += 1
                        #print(bytes([node.value]).decode("utf-8"), end='')
                #print('')
                ent = best.cum_cost/best_len
                avg = pow(2.0, -ent)
                ppl = pow(2.0, ent) # byte level
                ppll = pow(2.0, ent * best_len/best.length) # textequiv level (including spaces/newlines)
                logger.info("avg: %.3f, byte ppl: %.3f, %s ppl: %.3f", avg, ppl, level, ppll) # byte need not always equal glyph!
            else:
                textstring = u''.join(node[1][0].Unicode for node in text) # same length as text
                logger.info("Rating %d elements with a total of %d characters", len(text), len(textstring))
                confidences = self.rater.rate_once(textstring)
                i = 0
                for node in text:
                    textequiv = node[1][0] # 1st choice only
                    j = len(textequiv.Unicode)
                    conf = sum(confidences[i:i+j])/j
                    textequiv.set_conf(conf) # todo: incorporate input confidences, too (weighted product or as input into LM)
                    i += j
                if i != len(confidences):
                    logger.err("Input text length and output scores length are off by %d characters", i-len(confidences))
                avg = sum(confidences)/len(confidences)
                ent = sum([-log(max(p, 1e-99), 2) for p in confidences])/len(confidences)
                ppl = pow(2.0, ent) # character level
                ppll = pow(2.0, ent * len(confidences)/len(text)) # textequiv level (including spaces/newlines)
                logger.info("avg: %.3f, char ppl: %.3f, %s ppl: %.3f", avg, ppl, level, ppll) # char need not always equal glyph!
            ID = concat_padded(self.output_file_grp, n)
            self.workspace.add_file(
                ID=ID,
                file_grp=self.output_file_grp,
                basename=ID + '.xml',
                mimetype=MIMETYPE_PAGE,
                content=to_xml(pcgts),
            )

def filter_choices(textequivs):
    '''assuming `textequivs` are already sorted by input confidence (conf attribute), ensure maximum number and maximum relative threshold'''
    textequivs = textequivs[:min(CHOICE_THRESHOLD_NUM, len(textequivs))]
    if textequivs:
        conf0 = textequivs[0].conf
        if conf0:
            return [te for te in textequivs if conf0 - te.conf < CHOICE_THRESHOLD_CONF]
        else:
            return textequivs
    else:
        return []
