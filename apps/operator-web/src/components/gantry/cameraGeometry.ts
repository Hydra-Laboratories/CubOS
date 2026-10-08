export interface NormalizedPoint { x: number; y: number }
export interface ImageContentRect { left: number; top: number; width: number; height: number }

export function containedImageRect(
  containerWidth: number,
  containerHeight: number,
  imageWidth: number,
  imageHeight: number,
): ImageContentRect {
  if (containerWidth <= 0 || containerHeight <= 0 || imageWidth <= 0 || imageHeight <= 0) {
    return { left: 0, top: 0, width: containerWidth, height: containerHeight };
  }
  const scale = Math.min(containerWidth / imageWidth, containerHeight / imageHeight);
  const width = imageWidth * scale;
  const height = imageHeight * scale;
  return {
    left: (containerWidth - width) / 2,
    top: (containerHeight - height) / 2,
    width,
    height,
  };
}

export function pointInImage(
  clientX: number,
  clientY: number,
  containerLeft: number,
  containerTop: number,
  imageRect: ImageContentRect,
): NormalizedPoint | null {
  const x = clientX - containerLeft - imageRect.left;
  const y = clientY - containerTop - imageRect.top;
  if (x < 0 || y < 0 || x > imageRect.width || y > imageRect.height || imageRect.width <= 0 || imageRect.height <= 0) {
    return null;
  }
  return { x: x / imageRect.width, y: y / imageRect.height };
}
