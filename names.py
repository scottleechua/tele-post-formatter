import spacy

_nlp = None

def get_nlp():
    global _nlp
    if _nlp is None:
        _nlp = spacy.load("en_core_web_sm")
    return _nlp


def extract_names(text: str, ignored_names: list[str]) -> list[str]:
    """Extract unique PERSON entity names from text, excluding ignored names."""
    nlp = get_nlp()
    doc = nlp(text)

    ignored_lower = {n.lower() for n in ignored_names}
    seen = set()
    names = []

    for ent in doc.ents:
        if ent.label_ == "PERSON":
            name = ent.text.strip()
            key = name.lower()
            if key not in ignored_lower and key not in seen:
                seen.add(key)
                names.append(name)

    return names
