from io import BytesIO
from llama_index.core.schema import ImageDocument, ImageNode, NodeRelationship, RelatedNodeInfo, BaseNode, TextNode
from llama_index.core.workflow import Event,StartEvent,StopEvent,Workflow,step
from llama_index.core.workflow.errors import WorkflowRuntimeError
from llama_index.core.multi_modal_llms import MultiModalLLM
import logging
from PIL import Image
from sam2.automatic_mask_generator import SAM2ImagePredictor
from typing import Optional
import base64
import numpy as np
import shutil
import torch
import requests
from PIL import Image
import numpy as np
import torch
from transformers import AutoProcessor, Owlv2ForObjectDetection
from transformers.utils.constants import OPENAI_CLIP_MEAN, OPENAI_CLIP_STD

from .owl_v2 import Owlv2ProcessorWithNMS

class ImageRegion:
    def __init__(self, x1: int, y1: int, x2: int, y2: int, label: str, score: float):
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.label = label
        self.score = score

class ImageLoadedEvent(Event):
    image: ImageNode
    segmentation_configuration: dict | None
    object_detection_configuration: dict | None

class BBoxCreatedEvent(Event):
    """
    Event triggered when a bounding box is created for an image.

    Attributes:
        image (ImageNode): The image associated with the bounding box.
        segmentation_configuration (dict, optional): Configuration settings for image segmentation.
        object_detection_configuration (dict, optional): Configuration settings for object detection.
    """
    image: ImageNode
    segmentation_configuration: dict | None
    object_detection_configuration: dict | None

class ImageParsedEvent(Event):
    source: ImageNode
    chunks: list[ImageNode]

class ImageChunkGenerated(Event):
    image_node: ImageNode

class ImageNodeParserWorkflow(Workflow):
    _default_predictor_configuration = {
        "model_name": "facebook/sam2-hiera-small",
        "sam_settings": {}
        # "settings": {
        #     "points_per_side": 32,
        #     "points_per_batch": 128,
        #     "pred_iou_thresh": 0.7,
        #     "stability_score_thresh": 0.92,
        #     "stability_score_offset": 0.7,
        #     "crop_n_layers": 1,
        #     "box_nms_thresh": 0.7,
        #     "crop_n_points_downscale_factor": 2,
        #     "min_mask_region_area": 25.0,
        #     "use_m2m": True,
        #     "device_map": "cpu"
        # }
    },
    _object_detection_configuration = dict(
        confidence=0.1,
        nms_threshold=0.3
    )

    
    multi_modal_llm: Optional[MultiModalLLM] = None
    processor: Optional[AutoProcessor] = None
    model: Optional[Owlv2ForObjectDetection] = None

    def get_or_create_owl_v2(self) -> Owlv2ForObjectDetection:
        if self.model is None:
            self.model = Owlv2ForObjectDetection.from_pretrained("google/owlv2-large-patch14-ensemble")
        return self.model
    
    def get_or_create_owl_v2_processor(self) -> AutoProcessor:
        if self.processor is None:
            self.processor = Owlv2ProcessorWithNMS.from_pretrained("google/owlv2-large-patch14-ensemble")
        return self.processor

    @step()
    async def load_image(self, start_event: StartEvent) -> ImageLoadedEvent|StopEvent:
        """
        Load an image based on the provided event.

        Args:
            ev (StartEvent): The event containing the image information.

        Returns:
            ImageLaodedEvent: The event containing the loaded image.

        Raises:
            ValueError: If no image is provided.
        """

        sam_configuration = self._default_predictor_configuration
        object_detection_configuration = self._object_detection_configuration
        if hasattr(start_event, "segmentation_configuration") and start_event.segmentation_configuration is not None:
            sam_configuration = start_event.segmentation_configuration
        if hasattr(start_event, "image") and start_event.image is not None and isinstance(start_event.image, ImageNode):
            return ImageLoadedEvent(image=start_event.image, segmentation_configuration=sam_configuration)
        elif hasattr(start_event, "base64_image") and start_event.base64_image is not None:
            document = ImageDocument(image=start_event.base64_image, mimetype=start_event.mimetype, image_mimetype=start_event.mimetype)
            return ImageLoadedEvent(image=document, segmentation_configuration=sam_configuration)
        elif hasattr(start_event, "image_path") and start_event.image_path is not None:
            image = Image.open(start_event.image_path).convert("RGB")
            document = ImageDocument(image=self.image_to_base64(image), mimetype="image/jpg", image_mimetype="image/jpg")
            return ImageLoadedEvent(image=document, segmentation_configuration=sam_configuration, object_detection_configuration=object_detection_configuration)
        else:
            return StopEvent()
        
    @step()
    async def create_bboxes(self, image_laoded_event: ImageLoadedEvent) -> BBoxCreatedEvent:
        """
        Create bounding boxes for the image.
        """
        # Check if 'bbox_list' is already present in the segmentation configuration
            # Start of Selection
        try:
            if 'bbox_list' not in image_laoded_event.segmentation_configuration:
                # If 'prompt' is not present, generate prompts using the multi-modal LLM
                if 'prompt' not in image_laoded_event.segmentation_configuration:
                    prompt = self.multi_modal_llm.complete(
                        "Find the most important entities in the image and produce a list of short prompts to use for an object detection model. Put each single prompt on a new line. Emit only the prompts.",
                        [image_laoded_event.image]
                    )
                    # Store the generated prompts in the segmentation configuration
                    image_laoded_event.segmentation_configuration["prompt"] = prompt.text
                
                # Detect bounding boxes using Owlv2 with the specified prompt and configurations
                bbox_list = self._detect_bboxes_with_owlv2(
                    image_laoded_event.image,
                    image_laoded_event.segmentation_configuration['prompt'],
                    image_laoded_event.object_detection_configuration.get("confidence", 0.1),
                    image_laoded_event.object_detection_configuration.get("nms_threshold", 0.3)
                )
                
        except Exception as e:
            # Start Generation Here
            logging.error(f"Failed to create bounding boxes: {e}", exc_info=True)
            # Handle exceptions by logging the error and stopping the workflow
            return StopEvent(reason="Bounding box creation failed due to an error.")

        # Return the event with the updated segmentation configuration
        return BBoxCreatedEvent(image=image_laoded_event.image, segmentation_configuration=image_laoded_event.segmentation_configuration)


    @step()
    async def parse_image(self, bounding_boxes_created_event: BBoxCreatedEvent) -> ImageParsedEvent | StopEvent:
        """
        Parses the given image using the _parse_image_node_with_sam2 method.
        Parameters:
            ev (ImageLaodedEvent): The event containing the loaded image.
        Returns:
            StopEvent: The event containing the parsed image chunks.
        """
        parsed: list[ImageNode] = []

        parsed = self._parse_image_node_with_sam2(bounding_boxes_created_event.image, bounding_boxes_created_event.segmentation_configuration)

        if len(parsed) == 0:
            result = {
                "source": bounding_boxes_created_event.image,
                "chunks": []
            }
            return StopEvent(result=result)
        else:
            return ImageParsedEvent(source=bounding_boxes_created_event.image, chunks=parsed)
        
    @step()
    async def describe_image(self, image_parsed_event: ImageParsedEvent) -> StopEvent:
        """
        Generates descriptions for each chunk of the parsed image.

        This method iterates over the image chunks in the parsed event and uses a multi-modal
        language model to generate textual descriptions for each chunk. The descriptions are
        stored as TextNode instances with associated relationships to the source and parent nodes.

        Args:
            ev (ImageParsedEvent): The event containing the parsed image and its chunks.

        Returns:
            StopEvent: An event containing the source image, the image chunks, and their corresponding descriptions.
        """
        image_descriptions: list[TextNode] = []
        
        # Check if a multi-modal language model is available
        if self.multi_modal_llm is not None:
            for image_chunk in image_parsed_event.chunks:
                try:
                    # Generate a description for the current image chunk
                    image_description = self.multi_modal_llm.complete(
                        prompt="Describe the image above in a few words.",
                        image_documents=[
                            ImageDocument(
                                image=image_chunk.image,
                                mimetype=image_chunk.mimetype,
                                image_mimetype=image_chunk.mimetype
                            )
                        ],
                    )
                    # Create a TextNode for the description
                    image_description_node = TextNode(
                        text=image_description.text,
                        mimetype="text/plain"
                    )
                    # Establish relationships for the description node
                    image_description_node.relationships[NodeRelationship.SOURCE] = self._ref_doc_id(image_parsed_event.source)
                    image_description_node.relationships[NodeRelationship.PARENT] = image_chunk.as_related_node_info()
                    # Append the description node to the list
                    image_descriptions.append(image_description_node)
                except Exception:
                    # In case of an error, append None to maintain list length
                    image_descriptions.append(None)
          
        # Prepare the result dictionary with descriptions
        result = {
            "source": image_parsed_event.source,
            "chunks": image_parsed_event.chunks,
            "descriptions": image_descriptions
        }

        # Return the StopEvent with the result
        return StopEvent(result=result)


    def _detect_bboxes_with_owlv2(self, image_node: ImageNode, prompt: str, confidence: float, nms_threshold: float) -> list[ImageRegion]:
        """
        Detects stuff and returns the annotated image.
        Parameters:
            image: The input image (as numpy array).
            seg_input: The segmentation input (i.e. the prompt for the model).
            debug (bool): Flag to enable logging for debugging purposes.
        Returns:
            tuple: (numpy array of image, list of (label, (x1, y1, x2, y2)) tuples)
        """
    
        # Step 2: Detect stuff using owl_v2

        image = Image.open(image_node.resolve_image()).convert("RGB")
        processor = self.get_or_create_owl_v2_processor()
        model = self.get_or_create_owl_v2()


        texts = [[x.strip() for x in prompt.split("\n")]]
        inputs = processor(text=texts, images=image, return_tensors="pt")

        # forward pass
        with torch.no_grad():
            outputs = model(**inputs)

        # Note: boxes need to be visualized on the padded, unnormalized image
        # hence we'll set the target image sizes (height, width) based on that
        def get_preprocessed_image(pixel_values):
            pixel_values = pixel_values.squeeze().numpy()
            unnormalized_image = (pixel_values * np.array(OPENAI_CLIP_STD)[:, None, None]) + np.array(OPENAI_CLIP_MEAN)[:, None, None]
            unnormalized_image = (unnormalized_image * 255).astype(np.uint8)
            unnormalized_image = np.moveaxis(unnormalized_image, 0, -1)
            unnormalized_image = Image.fromarray(unnormalized_image)
            return unnormalized_image

        unnormalized_image = get_preprocessed_image(inputs.pixel_values)

        target_sizes = torch.Tensor([unnormalized_image.size[::-1]])
        # Convert outputs (bounding boxes and class logits) to final bounding boxes and scores
        results = processor.post_process_object_detection_with_nms(
            outputs=outputs, threshold=confidence, nms_threshold=nms_threshold, target_sizes=target_sizes
        )

        i = 0  # Retrieve predictions for the first image for the corresponding text queries
        text = texts[i]
        boxes, scores, labels = results[i]["boxes"], results[i]["scores"], results[i]["labels"]

        # Prepare annotations for AnnotatedImage output
        annotations: list[ImageRegion] = [] 
        for box, score, label in zip(boxes, scores, labels):
            box = [round(i, 2) for i in box.tolist()]
            print(f"Detected {text[label]} with confidence {round(score.item(), 3)} at location {box}")
            x1, y1, x2, y2 = box
            image_region = ImageRegion(x1, y1, x2, y2, label, score)
            annotations.append(image_region)
    
        return annotations


    def _parse_image_node_with_sam2(self, image_node: ImageNode, configuration : dict) -> list[ImageNode]:
        """
        Parses an image node by cropping it into smaller image chunks based on the provided annotations.
        Args:
            image_node (ImageNode): The image node to be parsed.
        Returns:
            list[ImageNode]: A list of image chunks generated from the cropping process.
        """
        img = Image.open(image_node.resolve_image()).convert("RGB")

        sam_settings = configuration.get("sam_settings", {})

        predictor = SAM2ImagePredictor.from_pretrained(configuration["model_name"], device="cpu", **sam_settings)

        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
            predictor.set_image(img)
                
            annotations = []
            for bbox in configuration["bbox_list"]:
                x1, y1, x2, y2 = bbox.x1, bbox.y1, bbox.x2, bbox.y2
                annotations.append(predictor.predict(box=(x1, y1, x2, y2)))

            # from each mask crop the image
            image_chunks = []
            for ann, bbox in zip(annotations, configuration["bbox_list"]):
                box = bbox.x1, bbox.y1, bbox.x2, bbox.y2
                mask = Image.fromarray(ann[0][-1].astype(np.uint8))
                masked_image = Image.composite(img, Image.new("RGB", img.size), mask)
                cropped_image = masked_image.crop(box)

                # save bounding box and ann to disk
                # Save bounding box and annotation to disk
                # bbox_filename = f"bbox_{bbox.label}_{bbox.score:.2f}.txt"
                # mask_filename = f"mask_{bbox.label}_{bbox.score:.2f}.png"
                
                # with open(bbox_filename, "w") as bbox_file:
                #     bbox_file.write(f"{bbox.x1},{bbox.y1},{bbox.x2},{bbox.y2},{bbox.label},{bbox.score}\n")
                
                # mask.save(mask_filename)
                
                x1, y1, x2, y2 = box
                region = dict(x1=x1, y1=y1, x2=x2, y2=y2)
                metadata = dict(region=region)
                try:
                    image_chunk = ImageNode(image=self.image_to_base64(cropped_image), mimetype=image_node.mimetype, metadata=metadata)
                    image_chunk.relationships[NodeRelationship.SOURCE] = self._ref_doc_id(image_node)
                    image_chunk.relationships[NodeRelationship.PARENT] = image_node.as_related_node_info()
                    image_chunks.append(image_chunk)
                    self.send_event(ImageChunkGenerated(image_node=image_chunk))
                except Exception as e:
                    self.send_event(WorkflowRuntimeError(e))
                    continue

        children_collection = image_node.relationships.get(NodeRelationship.CHILD, [])
        image_node.relationships[NodeRelationship.CHILD] = children_collection + [c.as_related_node_info() for c in image_chunks[1:]]
        
        return image_chunks

    def _ref_doc_id(self, node: BaseNode) -> RelatedNodeInfo:
        """
        Returns the related node information of the document for the given ImageNode.

        Parameters:
            node (ImageNode): The ImageNode for which to retrieve the related node information.

        Returns:
            RelatedNodeInfo: The related node information for the given ImageNode.
        """
        source_node = node.source_node
        if source_node is None:
            return node.as_related_node_info()
        return source_node
    
    def image_to_base64(self, pil_image, format="JPEG"):
        """
        Converts a PIL image to base64 string.

        Args:
            pil_image (PIL.Image.Image): The PIL image object to be converted.
            format (str, optional): The format of the image. Defaults to "JPEG".

        Returns:
            str: The base64 encoded string representation of the image.
        """
        buffered = BytesIO()
        pil_image.save(buffered, format=format)
        image_str = base64.b64encode(buffered.getvalue())
        return image_str.decode('utf-8') # Convert bytes to string