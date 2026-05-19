"""Combinatorial palette for Gemini image prompts.

Goal: 391 articles × 7 images each = 2700+ images. With fixed 6-prompt pools
per service all renovation/design articles look alike. This module composes
prompts from independent dimensions so visual variety scales combinatorially.

Dimensions (per-service relevant subset selected at runtime):
  • style       — 12 interior styles
  • room        — 10 room types
  • lighting    — 8 lighting moods
  • angle       — 6 camera angles / shot framings
  • mood        — 5 atmospheric descriptors
  • stage       — 7 construction stages (for renovation/construction services)

Combinations per service ≈ 12 × 10 × 8 × 6 = 5760 unique prompt seeds.
Plus randomized phrasing of fixed clauses → essentially unlimited variety.
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

# Service → relevant dimension hint (what should dominate)
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
        "rooms": ["modern residential building exterior",
                  "architectural drawings on desk",
                  "exterior courtyard with stone walls",
                  "blueprint review meeting space",
                  "model of building on table"],
        "styles": _STYLES[2:8],
        "include_stages": False,
    },
    "construction": {
        "rooms": ["building exterior", "construction site", "house framing"],
        "styles": _STYLES[:4],
        "include_stages": True,
        "stages": _STAGES_CONSTRUCTION,
    },
    "landscape": {
        "rooms": ["designed garden with stone path",
                  "outdoor patio with pergola",
                  "front yard with hedges",
                  "back terrace with fire pit",
                  "rock garden with cypresses",
                  "swimming pool with stone surround",
                  "outdoor kitchen and dining",
                  "vineyard-style backyard"],
        "styles": ["mediterranean landscape", "english country garden",
                   "japanese zen garden", "modern minimalist landscape",
                   "tropical lush garden", "desert succulent garden"],
        "include_stages": False,
    },
    "supervision": {
        "rooms": ["interior renovation site with designer",
                  "blueprint review on construction site",
                  "punch list inspection in modern apartment",
                  "client meeting in showroom",
                  "material samples spread on table"],
        "styles": _STYLES,
        "include_stages": False,
    },
    "completion": {
        "rooms": _ROOMS,
        "styles": _STYLES,
        "include_stages": False,
    },
    "furniture": {
        "rooms": _ROOMS,
        "styles": _STYLES,
        "include_stages": False,
    },
    "european-furniture": {
        "rooms": _ROOMS,
        "styles": ["italian classical luxury",
                   "spanish hand-crafted leather",
                   "portuguese ceramics and wood",
                   "scandinavian minimalist",
                   "french provincial style",
                   "german engineered modern"],
        "include_stages": False,
    },
    "curtains": {
        "rooms": ["tall window with flowing linen curtains",
                  "bedroom with blackout curtains",
                  "living room with sheer curtains",
                  "kitchen window with cafe curtains",
                  "fabric samples on designer's table",
                  "automated motorized curtains track"],
        "styles": _STYLES[:8],
        "include_stages": False,
    },
    "plaster": {
        "rooms": ["accent wall with venetian plaster",
                  "feature wall with concrete-effect plaster",
                  "plastered ceiling with subtle texture",
                  "plaster samples side by side on wall",
                  "artist applying decorative plaster"],
        "styles": _STYLES,
        "include_stages": False,
    },
    "panels": [
        "wall with WPC wood panels",
        "feature wall with bamboo panels",
        "SPC panels on stairs and hallway",
        "WPC ceiling panels in bedroom",
        "panel installation in progress",
        "panel samples in showroom",
    ],
    "flexstone": [
        "exterior facade with flexible stone tiles",
        "curved wall with flex stone in lobby",
        "outdoor archway covered in flex stone",
        "interior accent wall with stone tile",
        "stone tile installation on curved surface",
        "bathroom with flex stone shower wall",
    ],
}


def build_random_prompt(
    article: Any,
    seed_index: int = 0,
    section_context: str = "",
) -> str:
    """Build one randomized Gemini image prompt for given article + section.

    Args:
        article: roadmap Article (uses .h1, .service, .geo)
        seed_index: 0 for cover, 1..N for inline images.
            Same article+seed_index always returns same prompt (deterministic
            within one publish so retries reuse cached image).
        section_context: optional h2 title for hint relevance.

    Returns: full prompt string ~250-400 chars.
    """
    service_slug = (getattr(article, "service", "") or "design").lower()
    hint = _SERVICE_HINTS.get(service_slug)

    # Service-specific palettes (or fallback to generic)
    if isinstance(hint, dict):
        rooms = hint.get("rooms", _ROOMS)
        styles = hint.get("styles", _STYLES)
        include_stages = hint.get("include_stages", False)
        stages = hint.get("stages", [])
    elif isinstance(hint, list):
        # Old format — just a list of scenes
        rooms = hint
        styles = _STYLES[:4]
        include_stages = False
        stages = []
    else:
        rooms = _ROOMS
        styles = _STYLES
        include_stages = False
        stages = []

    # Deterministic seed: article.id + seed_index ensures same prompt on retry
    article_id = int(getattr(article, "id", 0) or 0)
    rng = random.Random(article_id * 100 + seed_index)

    style = rng.choice(styles)
    room = rng.choice(rooms)
    light = rng.choice(_LIGHTING)
    angle = rng.choice(_ANGLES)
    mood = rng.choice(_MOODS)

    parts = [
        f"Professional editorial photograph for blog article.",
        f"Subject: {room}, {style}.",
    ]

    # Add stage variation (only for renovation/construction)
    if include_stages and stages and rng.random() < 0.40:  # 40% chance
        stage = rng.choice(stages)
        parts.append(f"Stage: {stage}.")

    parts.append(f"Lighting: {light}.")
    parts.append(f"Composition: {angle}, {mood} atmosphere.")

    if section_context:
        # Soft hint at section topic without forcing literal interpretation
        sc = section_context[:80]
        parts.append(f"Context relevance: {sc}.")

    geo = (getattr(article, "geo", "") or "Крым").lower()
    if "крым" in geo or not geo:
        parts.append("Setting: Crimea / Mediterranean coastal context.")

    parts.append(
        "Photography style: realistic, magazine quality, sharp focus, "
        "shallow depth of field. No text overlays, no logos, no watermarks, "
        "no people in foreground. Aspect ratio 16:9, 1024x576."
    )

    return " ".join(parts)
