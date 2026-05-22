"""Combinatorial palette for Gemini image prompts.

Goal: 391 articles x 7 images each = 2700+ images. With fixed prompt pools
per service all renovation/design articles look alike. This module composes
prompts from independent dimensions so visual variety scales combinatorially,
AND keeps the subject locked to what each service actually is about
(custom furniture = kitchens/wardrobes/closets, not bathtubs or sofas).

Dimensions (per-service relevant subset selected at runtime):
- subject  - service-locked scene (what the article is about)
- style    - 12 interior styles
- palette  - 12 colour schemes
- lighting - 8 lighting moods
- angle    - 6 camera angles / shot framings
- mood     - 5 atmospheric descriptors
- stage    - 7 construction stages (for renovation/construction services)

The N images of one article are forced onto DIFFERENT subjects + palettes
(deterministic per-article shuffle) so they no longer look alike.
"""

from __future__ import annotations

import random
from typing import Any

_STYLES = [
    "classical interior with crown molding and panel walls",
    "minimalist scandinavian style, light wood and neutral tones",
    "industrial loft style, exposed brick and metal beams",
    "modern contemporary style with clean lines and large windows",
    "art deco style with geometric patterns and brass accents",
    "japanese wabi-sabi style with natural materials and shoji screens",
    "provence style with whitewashed wood and lavender accents",
    "boho eclectic style with layered textiles and plants",
    "high-tech style with glass, steel and integrated technology",
    "eco style with reclaimed wood, plants and natural fiber rugs",
    "mid-century modern style with teak furniture and warm tones",
    "mediterranean coastal style with white stucco and blue accents",
]

# Generic rooms - used ONLY by services whose subject really is "any room"
# (interior design, renovation).
_ROOMS = [
    "living room",
    "bedroom",
    "kitchen with island",
    "bathroom with freestanding tub",
    "home office study",
    "dining area with statement table",
    "hallway entrance foyer",
    "children's room",
    "open-plan living-dining space",
    "terrace or balcony with view",
]

_PALETTES = [
    "warm oak and cream tones",
    "crisp white and soft grey palette",
    "deep green and brass accents",
    "walnut wood and matte black",
    "beige, taupe and natural linen",
    "navy blue and warm gold details",
    "terracotta and sand tones",
    "monochrome white and light grey",
    "sage green and natural wood",
    "charcoal grey and warm wood",
    "soft pastel tones with light wood",
    "black, white and chrome contrast",
]

_LIGHTING = [
    "golden hour soft natural light through large windows",
    "blue hour twilight with warm interior lamps glowing",
    "bright midday daylight with sharp shadows",
    "overcast diffused light, soft and even",
    "early morning sunrise light with pastel tones",
    "evening warm lamp light with cozy atmosphere",
    "dramatic side light with deep shadows",
    "north-facing window cool diffused light",
]

_ANGLES = [
    "wide-angle architectural shot showing entire room",
    "medium shot focusing on key furniture pieces",
    "low-angle shot emphasizing ceiling height",
    "eye-level perspective from doorway",
    "corner shot showing two walls and floor",
    "elevated three-quarter view",
]

_MOODS = [
    "calm and serene",
    "vibrant and energetic",
    "intimate and cozy",
    "elegant and refined",
    "raw and authentic",
]

_STAGES_RENOVATION = [
    "early demolition stage with bare walls and dust",
    "rough electrical wiring exposed in walls",
    "fresh plaster being applied to walls",
    "tiling work in progress on bathroom floor",
    "newly installed flooring with protective covering",
    "final paintwork and trim installation",
    "finished space staged with furniture",
]

_STAGES_CONSTRUCTION = [
    "foundation pouring with concrete forms",
    "framing stage with wooden roof structure",
    "exterior walls with scaffolding",
    "roofing installation in progress",
    "interior dry-wall installation",
    "exterior finish stage with siding",
    "completed exterior of new private house",
]

# Service -> relevant dimensions. "rooms" here means the SUBJECT pool and is
# locked to what the service actually delivers.
_SERVICE_HINTS = {
    "design": {
        "rooms": _ROOMS,
        "styles": _STYLES,
        "include_stages": False,
    },
    "renovation": {
        "rooms": _ROOMS,
        "styles": _STYLES[:6],
        "include_stages": True,
        "stages": _STAGES_RENOVATION,
    },
    "architecture": {
        "rooms": [
            "modern residential building exterior",
            "private cottage exterior with landscaping",
            "architectural drawings and floor plans on a desk",
            "exterior courtyard with stone walls",
            "blueprint review over a large desk",
            "scale model of a building on a table",
            "3D massing model of a house",
        ],
        "styles": _STYLES[2:8],
        "include_stages": False,
    },
    "construction": {
        "rooms": [
            "new private house exterior, recently completed",
            "house under construction with scaffolding",
            "construction site of a private home",
            "house framing and roof structure",
            "brickwork of exterior walls in progress",
            "completed two-storey private house with finished facade",
            "concrete foundation and groundworks of a new house",
        ],
        "styles": _STYLES[:4],
        "include_stages": True,
        "stages": _STAGES_CONSTRUCTION,
    },
    "landscape": {
        "rooms": [
            "designed garden with stone path",
            "outdoor patio with pergola",
            "front yard with hedges",
            "back terrace with fire pit",
            "rock garden with cypresses",
            "swimming pool with stone surround",
            "outdoor kitchen and dining",
            "vineyard-style backyard",
        ],
        "styles": [
            "mediterranean landscape",
            "english country garden",
            "japanese zen garden",
            "modern minimalist landscape",
            "tropical lush garden",
            "desert succulent garden",
        ],
        "include_stages": False,
    },
    "supervision": {
        "rooms": [
            "interior renovation site with a designer reviewing work",
            "blueprint review on a construction site",
            "punch-list inspection in a modern apartment under finishing",
            "designer checking finishes against drawings on site",
            "material samples spread on a table on site",
            "designer and foreman discussing drawings on site",
            "quality check of tiling and finishes on site",
        ],
        "styles": _STYLES,
        "include_stages": False,
    },
    "completion": {
        "rooms": [
            "finished styled living room with curated decor and accessories",
            "furniture and decor selection with material sample boards",
            "newly furnished bedroom staged with textiles and lighting",
            "designer arranging decor items and accessories on shelves",
            "showroom selection of furniture and lighting fixtures",
            "finishes and materials mood board laid out on a table",
            "styled dining area with tableware and decor",
            "delivered furniture being arranged in a new apartment",
        ],
        "styles": _STYLES,
        "include_stages": False,
    },
    "furniture": {
        "rooms": [
            "custom fitted kitchen cabinetry with island",
            "built-in wardrobe with sliding doors",
            "walk-in closet with custom shelving systems",
            "fitted hallway storage and shoe cabinetry",
            "custom TV media wall with cabinetry",
            "built-in bookshelves and storage wall",
            "custom bathroom vanity cabinet unit",
            "children's room with fitted storage furniture",
            "modern kitchen with tall pantry cabinets",
            "dressing room with open wardrobe systems",
            "home office with built-in desk and shelving",
            "laundry and utility room cabinetry",
        ],
        "styles": _STYLES,
        "include_stages": False,
    },
    "european-furniture": {
        "rooms": [
            "luxury italian sofa in an elegant living room",
            "designer armchairs around a marble coffee table",
            "premium dining set with upholstered chairs",
            "european leather sofa and lounge seating",
            "designer bedroom with an upholstered bed",
            "showroom display of premium upholstered furniture",
            "living room with a designer modular sofa",
            "elegant lounge area with a statement armchair",
        ],
        "styles": [
            "italian classical luxury",
            "spanish hand-crafted leather",
            "portuguese ceramics and wood",
            "scandinavian minimalist",
            "french provincial style",
            "german engineered modern",
        ],
        "include_stages": False,
    },
    "curtains": {
        "rooms": [
            "tall window with flowing linen curtains",
            "bedroom with blackout curtains",
            "living room with sheer curtains",
            "kitchen window with cafe curtains",
            "fabric samples on a designer's table",
            "automated motorized curtain track",
            "roman blinds on a large window",
        ],
        "styles": _STYLES[:8],
        "include_stages": False,
    },
    "plaster": {
        "rooms": [
            "accent wall with venetian plaster",
            "feature wall with concrete-effect plaster",
            "plastered ceiling with subtle texture",
            "decorative plaster samples side by side on a wall",
            "close-up of textured decorative plaster finish",
            "hallway with polished venetian plaster walls",
            "living room feature wall with travertine-effect plaster",
        ],
        "styles": _STYLES,
        "include_stages": False,
    },
    "panels": {
        "rooms": [
            "wall with WPC wood panels",
            "feature wall with bamboo panels",
            "SPC panels on stairs and hallway",
            "WPC ceiling panels in a bedroom",
            "decorative slat wall panels in a living room",
            "wall panel samples in a showroom",
        ],
        "styles": _STYLES[:8],
        "include_stages": False,
    },
    "flexstone": {
        "rooms": [
            "exterior facade with flexible stone tiles",
            "curved wall with flexible stone in a lobby",
            "outdoor archway covered in flexible stone",
            "interior accent wall with large-format ceramic",
            "flexible stone cladding on a curved surface",
            "bathroom with flexible stone shower wall",
        ],
        "styles": _STYLES[:8],
        "include_stages": False,
    },
}


def build_random_prompt(
    article: Any,
    seed_index: int = 0,
    section_context: str = "",
) -> str:
    """Build one randomized Gemini image prompt for given article + section.

    Args:
        article: roadmap Article (uses .id, .service)
        seed_index: 0 for cover, 1..N for inline images.
            Same article+seed_index always returns the same prompt
            (deterministic within one publish so retries reuse cached image).
        section_context: optional h2 title for hint relevance.

    Returns: full prompt string ~250-420 chars.
    """
    service_slug = (getattr(article, "service", "") or "design").lower()
    hint = _SERVICE_HINTS.get(service_slug)

    if isinstance(hint, dict):
        rooms = hint.get("rooms", _ROOMS)
        styles = hint.get("styles", _STYLES)
        include_stages = hint.get("include_stages", False)
        stages = hint.get("stages", [])
    elif isinstance(hint, list):
        rooms = hint
        styles = _STYLES[:4]
        include_stages = False
        stages = []
    else:
        rooms = _ROOMS
        styles = _STYLES
        include_stages = False
        stages = []

    article_id = int(getattr(article, "id", 0) or 0)

    # Per-article deterministic shuffle: the images of one article are forced
    # onto DIFFERENT subjects and palettes instead of all looking alike.
    arr = random.Random(article_id * 7 + 1)
    rooms_shuffled = list(rooms)
    arr.shuffle(rooms_shuffled)
    palettes_shuffled = list(_PALETTES)
    arr.shuffle(palettes_shuffled)

    # Per-image rng for the remaining free axes (deterministic on retry).
    rng = random.Random(article_id * 100 + seed_index)

    room = rooms_shuffled[seed_index % len(rooms_shuffled)]
    palette = palettes_shuffled[seed_index % len(palettes_shuffled)]
    style = rng.choice(styles)
    light = rng.choice(_LIGHTING)
    angle = rng.choice(_ANGLES)
    mood = rng.choice(_MOODS)

    parts = [
        "Professional editorial photograph for a blog article.",
        f"Subject: {room}, {style}.",
        f"Color palette: {palette}.",
    ]

    if include_stages and stages and rng.random() < 0.40:
        stage = rng.choice(stages)
        parts.append(f"Stage: {stage}.")

    parts.append(f"Lighting: {light}.")
    parts.append(f"Composition: {angle}, {mood} atmosphere.")

    if section_context:
        sc = section_context[:80]
        parts.append(f"Context relevance: {sc}.")

    # All Designservice blog articles target Crimea; keep coastal context.
    parts.append("Setting: Crimea / Mediterranean coastal context.")

    parts.append(
        "Photography style: realistic, magazine quality, sharp focus, "
        "shallow depth of field. No text overlays, no logos, no watermarks, "
        "no people in foreground. Aspect ratio 16:9, 1024x576."
    )

    return " ".join(parts)
