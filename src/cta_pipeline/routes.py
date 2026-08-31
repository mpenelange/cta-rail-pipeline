import re

ROUTES={
    "Red":("Red Line",196,1),
    "Blue":("Blue Line",27,4),
    "Brn":("Brown Line",94,3),
    "G":("Green Line",34,2),
    "Org":("Orange Line",208,3),
    "P":("Purple Line",129,5),
    "Pexp":("Purple Line Express",129,5),
    "Pink":("Pink Line",205,5),
    "Y":("Yellow Line",226,3),
}
ALIASES={code.casefold():name for code,(name,_color,_fallback) in ROUTES.items()}
ALIASES.update({name.casefold():name for name,_color,_fallback in ROUTES.values()})
ALIASES.update({name.removesuffix(" Line").casefold():name for name,_color,_fallback in ROUTES.values()})
ROUTE_NAMES=tuple(dict.fromkeys(name for name,_color,_fallback in ROUTES.values()))

def route_name(value):
    text=str(value).strip(); return ALIASES.get(text.casefold(),text)

def resolve_route(query):
    normalized=" ".join(re.findall(r"[a-z0-9]+",query.casefold()))
    matches=[]
    for code,(name,color,_fallback) in ROUTES.items():
        aliases={code.casefold(),name.casefold(),name.removesuffix(" Line").casefold()}
        if any(re.search(r"\b"+re.escape(alias)+r"\b",normalized) for alias in aliases): matches.append({"route_id":code,"name":name,"color":color})
    unique={row["name"]:row for row in matches}
    if len(unique)==1: return next(iter(unique.values()))
    if not unique: return None
    return None

def route_clarification():
    options=[]; seen=set()
    for code,(name,_color,_fallback) in ROUTES.items():
        if name in seen or code=="Pexp": continue
        seen.add(name); options.append({"id":code,"label":name})
    return {"type":"clarification","field":"route_id","question":"Which rail line do you mean?","options":options}

def route_from_id(route_id):
    value=ROUTES.get(route_id)
    return {"route_id":route_id,"name":value[0],"color":value[1]} if value else None

def display_station_name(value):
    match=re.fullmatch(r"(.+?) \(([^)]+)\)",value)
    if not match: return value
    qualifier=match.group(2)
    tokens=sorted((alias for alias in ALIASES if " " not in alias),key=len,reverse=True)
    expanded=re.sub(r"\b("+"|".join(re.escape(token) for token in tokens)+r")\b",lambda found:route_name(found.group()),qualifier,flags=re.IGNORECASE)
    return f"{match.group(1)} — {expanded}" if expanded!=qualifier else value
