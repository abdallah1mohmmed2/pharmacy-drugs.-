"""Conservative packaging-name facts from Egyptian drug catalogue.

Not clinical dosing information. Missing/unclear cues stay unknown. Evidence
is only the product name, never a drug-class inference.
"""
from __future__ import annotations
import re

FORM_PATTERNS = (
    ('TABLET', r'\b(?:tablets?|tabs?\.?|f\.?c\.?\s*tabs?|s\.?r\.?\s*tabs?)\b'),
    ('CAPSULE', r'\b(?:capsules?|caps?\.?)\b'),
    ('SYRUP', r'\bsyrups?\b'),
    ('SUSPENSION', r'\b(?:suspension|susp\.?|oral\s+suspension)\b'),
    ('CREAM', r'\bcreams?\b'),
    ('OINTMENT', r'\b(?:ointment|oint\.?)\b'),
    ('GEL', r'\bgels?\b'),
    ('SUPPOSITORY', r'\b(?:suppositor(?:y|ies)|supp\.?|pessary|pessaries)\b'),
    ('INJECTION', r'\b(?:injection|inj\.?|ampoules?|ampules?|vials?)\b'),
    ('DROPS', r'\bdrops?\b'),
    ('SPRAY', r'\bsprays?\b'),
    ('INHALER', r'\b(?:inhalers?|inhalation|nebulizer|nebuliser)\b'),
    ('PATCH', r'\bpatch(?:es)?\b'),
    ('LOTION', r'\blotions?\b'),
    ('SACHET', r'\b(?:sachets?|sache?s?)\b'),
    ('POWDER', r'\bpowders?\b'),
    ('SOLUTION', r'\b(?:solutions?|soln\.?)\b'),
)
# Avoid bare "g": for example "20g" often means cream tube content, not strength.
STRENGTH = re.compile(
    r'(?<![\w/])(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mcg|µg|ug|mg|iu|i\.u\.|%)(?!\w)',
    re.I,
)


def product_name_fields(name: str, ingredients: str = "") -> tuple[dict, dict]:
    """Return safe target fields and per-field evidence; never guess a dose."""
    text = str(name or '').lower().replace('μg', 'µg')
    result = {}
    evidence = {}
    forms = [(code, m.group()) for code, pat in FORM_PATTERNS
             if (m := re.search(pat, text, flags=re.I))]
    recognized = {f for f, _ in forms}
    if 'INJECTION' in recognized and recognized <= {'INJECTION', 'POWDER', 'SOLUTION'}:
        form = 'INJECTION'
    elif len(recognized) == 1:
        form = next(iter(recognized))
    else:
        form = None
    if form:
        result['dosage_form'] = form
        evidence['dosage_form'] = 'name:' + ', '.join(sorted(set(m for _, m in forms)))

    route = None
    if re.search(r'\b(?:ophthalmic|eye\s*(?:drops?|ointments?|gels?|creams?)|ocular)\b', text):
        route = 'OPHTHALMIC'
    elif re.search(r'\b(?:otic|ear\s*(?:drops?|ointments?|gels?))\b', text):
        route = 'OTIC'
    elif re.search(r'\b(?:vaginal|pessary|pessaries)\b', text):
        route = 'VAGINAL'
    elif re.search(r'\b(?:rectal|suppository|suppositories)\b', text):
        route = 'RECTAL'
    elif re.search(r'\b(?:nasal|nose\s*spray)\b', text):
        route = 'NASAL'
    elif re.search(r'\b(?:intravenous|i\.?v\.?|intramuscular|i\.?m\.?)\b', text):
        route = 'PARENTERAL'
    elif re.search(r'\b(?:inhalation|inhaler|nebulizer|nebuliser)\b', text):
        route = 'INHALATION'
    elif form in {'TABLET', 'CAPSULE', 'SYRUP', 'SUSPENSION'}:
        route = 'ORAL'
    elif form in {'CREAM', 'OINTMENT', 'GEL', 'LOTION', 'PATCH'}:
        route = 'TOPICAL'
    if route:
        result['route'] = route
        evidence['route'] = f'name: route/form cue ({route})'

    # Avoid a single numeric value for combination products and ratios:
    # 10/25mg, 5mg/2ml, 10mg+20mg, etc.
    found = list(STRENGTH.finditer(text))
    multi_ingredient = any(sep in str(ingredients or '') for sep in ('+', ';', '/'))
    concentration_or_combo = bool(re.search(
        r'\d\s*/\s*\d|(?:mg|mcg|ug|µg|g|iu)\s*/\s*(?:\d+(?:\.\d+)?\s*)?(?:ml|l|g|mg|mcg)|\d\s*(?:mg|mcg|ug|µg|g)\s*\+',
        text, re.I,
    ))
    if len(found) == 1 and not concentration_or_combo and not multi_ingredient:
        m = found[0]
        number = float(m.group('value'))
        unit = m.group('unit').lower().replace('µg', 'mcg').replace('ug', 'mcg').replace('i.u.', 'iu')
        if 0 < number < 1000000:
            result['strength_text'] = m.group().strip()
            result['strength_value'] = number
            result['strength_unit'] = unit
            for key in ('strength_text', 'strength_value', 'strength_unit'):
                evidence[key] = 'name:' + m.group().strip()
    return result, evidence
