from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw


BASE_DIR = Path(__file__).resolve().parent.parent
MOCKUP_PATH = BASE_DIR / "assets" / "manga_update_mockup.png"

IMAGE_X = 984
IMAGE_Y = 82
IMAGE_WIDTH = 539
IMAGE_HEIGHT = 773
IMAGE_ZOOM = 1.02
IMAGE_BORDER_RADIUS = 28


async def render_manga_update_mockup(image_url: str) -> BytesIO:
    image_url = str(image_url or "").strip()
    if not image_url:
        raise ValueError("URL da capa nao informada.")
    if not MOCKUP_PATH.exists():
        raise FileNotFoundError(f"Mockup nao encontrado: {MOCKUP_PATH}")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(image_url)
        response.raise_for_status()

    mockup = Image.open(MOCKUP_PATH).convert("RGBA")
    cover = Image.open(BytesIO(response.content)).convert("RGBA")

    ratio = max(IMAGE_WIDTH / cover.width, IMAGE_HEIGHT / cover.height)
    new_width = int(cover.width * ratio * IMAGE_ZOOM)
    new_height = int(cover.height * ratio * IMAGE_ZOOM)
    cover = cover.resize((new_width, new_height), Image.Resampling.LANCZOS)

    left = max(0, (new_width - IMAGE_WIDTH) // 2)
    top = max(0, (new_height - IMAGE_HEIGHT) // 2)
    cover = cover.crop((left, top, left + IMAGE_WIDTH, top + IMAGE_HEIGHT))

    mask = Image.new("L", (IMAGE_WIDTH, IMAGE_HEIGHT), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        [0, 0, IMAGE_WIDTH, IMAGE_HEIGHT],
        radius=IMAGE_BORDER_RADIUS,
        fill=255,
    )
    cover.putalpha(mask)

    mockup.paste(cover, (IMAGE_X, IMAGE_Y), cover)

    output = BytesIO()
    output.name = "novo_capitulo_manga.png"
    mockup.convert("RGB").save(output, format="PNG", optimize=True)
    output.seek(0)
    return output
