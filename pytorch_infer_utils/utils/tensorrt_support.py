import os
from collections.abc import Iterator
from typing import Any, Callable, List, Optional, Tuple

import cv2
import numpy as np
import pycuda.autoinit
import pycuda.driver as cuda
import tensorrt as trt
from PIL import Image


def check_tensorrt_health() -> None:
    import tensorrt as trt

    print(f"[TensorRT] INFO: Use version {trt.__version__}")
    assert trt.Builder(trt.Logger()), "TensorRT is not valid."


def read_img(img_path: str) -> np.ndarray:
    img = Image.open(img_path).convert("RGB")
    img = np.array(img)
    return img


def process_img(
    img_rgb: np.ndarray,
    img_width: int = 224,
    img_height: int = 224,
    interp: int = cv2.INTER_LINEAR,
    max_pixel_value: int = 255,
    mean_channels: Tuple[float, ...] = (0.485, 0.456, 0.406),
    std_channels: Tuple[float, ...] = (0.229, 0.224, 0.225),
) -> np.ndarray:
    img = cv2.resize(
        img_rgb, (img_width, img_height), interpolation=interp
    )
    mean = max_pixel_value * np.array(mean_channels, dtype=np.float32)
    scale = 1 / (max_pixel_value * np.array(std_channels, dtype=np.float32))
    img = (img.astype(np.float32) - mean) * scale

    img = img.transpose((2, 0, 1))
    img = np.ascontiguousarray(img, dtype=img.dtype)
    return img


def load_img(image: Any, **kwargs) -> Any:
    if isinstance(image, str):
        img = read_img(image)
        img = process_img(img, **kwargs)
        return img
    return image


class BatchStream(Iterator):
    def __init__(
        self,
        calibration_set: List[Any],
        batch_size: int,
        max_item_shape: List[int],
        load_item_func: Optional[Callable] = None,
        verbose: bool = False,
    ) -> None:
        self.batch_size = batch_size
        self.calibration_set = calibration_set
        self.load_item_func = load_item_func
        self.max_batches = np.ceil(
            float(len(self.calibration_set)) / self.batch_size
        )
        self.shape = [batch_size] + max_item_shape
        self.max_batches = int(self.max_batches)
        self.batch = 0
        self.verbose = verbose

    @classmethod
    def reset(cls) -> None:
        cls.batch = 0

    def __next__(self) -> np.ndarray:
        if self.max_batches <= self.batch:
            raise StopIteration

        curr_batch = list()
        start = self.batch_size * self.batch
        stop = start + self.batch_size
        alloc_items = self.calibration_set[start:stop]
        for item in alloc_items:
            if self.load_item_func:
                item = self.load_item_func(item)
            curr_batch.append(item)

        self.batch += 1
        if self.verbose:
            print(f"[BatchStream] INFO: Load batch #{self.batch}.")
        return np.ascontiguousarray(curr_batch, dtype=np.float32)


class EntropyCalibrator(trt.IInt8EntropyCalibrator2):
    def __init__(self, stream: BatchStream, cache_file: str) -> None:
        # Whenever it specifies a custom constructor for a TensorRT class,
        # required calling the constructor of the parent explicitly.
        trt.IInt8EntropyCalibrator2.__init__(self)

        self.stream = stream
        self.cache_file = cache_file
        self.device_input = cuda.mem_alloc(
            trt.volume(self.stream.shape) * trt.float32.itemsize
        )
        stream.reset()

    def get_batch_size(self) -> int:
        return self.stream.batch_size

    def get_batch(
        self, names: List[str], p_str: Any = None
    ) -> Optional[List[int]]:
        """
        Before: def get_batch(self, bindings, names):
        TensorRT passes along the names of the engine
        bindings to the get_batch function. It doesn't
        necessarily have to use them, but they can be
        useful to understand the order of the inputs.
        The bindings list is expected to have the same
        ordering as 'names'.

        https://docs.nvidia.com/deeplearning/tensorrt/api/
        python_api/infer/Int8/EntropyCalibrator2.html
        #tensorrt.IInt8EntropyCalibrator2.get_batch
        """
        try:
            # Assume self.batches is a generator that
            # provides batch data.
            batch = next(self.stream)
            # Assume that self.device_input is a device
            # buffer allocated by the constructor.
            cuda.memcpy_htod(self.device_input, batch)
            return [int(self.device_input)]

        except StopIteration:
            # When it's out of batches, it returns
            # either [] or None. This signals to
            # TensorRT that there is no calibration
            # data remaining.
            return None

    def read_calibration_cache(self) -> Any:
        # If there is a cache, use it instead of calibrating again.
        # Otherwise, implicitly return None.
        if os.path.exists(self.cache_file):
            with open(self.cache_file, "rb") as f:
                return f.read()

    def write_calibration_cache(self, cache: Any) -> None:
        with open(self.cache_file, "wb") as f:
            f.write(cache)
