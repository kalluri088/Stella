"""Provider-neutral one-shot image understanding boundary."""

from abc import ABC, abstractmethod

from stella.context import InputPart


class VisionProvider(ABC):
    """Interface for one bounded image-to-text observation request."""

    @abstractmethod
    def describe(self, image: InputPart) -> str:
        """Return bounded derived text for one image input part."""
