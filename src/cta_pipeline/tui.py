import curses
import re
import textwrap
from .routes import ROUTES,ROUTE_NAMES

BLUE=1; RED=2; AMBER=3; MUTED=4; GREEN=5
ROUTE_PAIR_START=10

def _put(screen,row,column,text,style=0):
    height,width=screen.getmaxyx()
    if 0<=row<height and column<width:
        try: screen.addnstr(row,column,text,max(0,width-column-1),style)
        except curses.error: pass

def _wrapped(text,width):
    lines=[]
    for paragraph in str(text).splitlines() or [""]:
        lines.extend(textwrap.wrap(paragraph,max(10,width),replace_whitespace=False) or [""])
    return lines

def _question_lines(question,width):
    """Wrap editable input with a hanging prompt and predictable cursor position."""
    parts=textwrap.wrap(question,max(1,width-2),replace_whitespace=False,drop_whitespace=False) or [""]
    return [("> " if index==0 else "  ")+part for index,part in enumerate(parts)]

def _route_pair(name):
    names=list(dict.fromkeys(route for route,_color,_fallback in ROUTES.values()))
    return ROUTE_PAIR_START+names.index(name) if name in names else 0

def _put_routes(screen,row,column,text,base_style=0):
    pattern="("+"|".join(re.escape(name) for name in sorted(ROUTE_NAMES,key=len,reverse=True))+")"
    offset=0
    for part in re.split(pattern,text):
        if not part: continue
        style=(curses.color_pair(_route_pair(part))|curses.A_BOLD) if part in ROUTE_NAMES else base_style
        _put(screen,row,column+offset,part,style); offset+=len(part)

class TUI:
    def __init__(self,service):
        self.service=service; self.question=""; self.result=None; self.selection=0; self.busy=False; self.selections={}
    def run(self,screen):
        curses.curs_set(1); screen.keypad(True); curses.use_default_colors()
        curses.init_pair(BLUE,curses.COLOR_CYAN,-1); curses.init_pair(RED,curses.COLOR_RED,-1); curses.init_pair(AMBER,curses.COLOR_YELLOW,-1); curses.init_pair(MUTED,curses.COLOR_WHITE,-1); curses.init_pair(GREEN,curses.COLOR_GREEN,-1)
        names=list(dict.fromkeys(route for route,_color,_fallback in ROUTES.values()))
        for index,name in enumerate(names):
            color=next((color if curses.COLORS>=256 else fallback for route,color,fallback in ROUTES.values() if route==name),curses.COLOR_WHITE)
            curses.init_pair(ROUTE_PAIR_START+index,color,-1)
        while True:
            self.draw(screen); key=screen.get_wch()
            if key=="\x03" or key=="\x1b": return
            if key=="\x12": self.reset(); continue
            if self.result and self.result.get("type")=="clarification":
                options=self.result["options"]
                if key in (curses.KEY_UP,"k"): self.selection=(self.selection-1)%len(options)
                elif key in (curses.KEY_DOWN,"j"): self.selection=(self.selection+1)%len(options)
                elif key in ("\n","\r",curses.KEY_ENTER): self.submit(options[self.selection]["id"],screen)
                elif key in (curses.KEY_BACKSPACE,"\b","\x7f"): self.result=None; self.selections={}
                continue
            if key in ("\n","\r",curses.KEY_ENTER): self.submit(screen=screen)
            elif key in (curses.KEY_BACKSPACE,"\b","\x7f"): self.question=self.question[:-1]; self.selections={}
            elif isinstance(key,str) and key.isprintable() and len(self.question)<1000: self.question+=key; self.selections={}
    def reset(self):
        self.question=""; self.result=None; self.selection=0; self.busy=False; self.selections={}
    def submit(self,choice_id=None,screen=None):
        if not self.question.strip(): return
        self.busy=True
        if choice_id is not None and self.result and self.result.get("type")=="clarification": self.selections[self.result["field"]]=choice_id
        elif choice_id is None and (not self.result or self.result.get("type")!="clarification"): self.selections={}
        try:
            if screen is not None: self.draw(screen)
            self.result=self.service.ask(self.question,self.selections); self.selection=0
        except Exception as error: self.result={"type":"error","answer":str(error) or "The request could not be completed."}
        finally: self.busy=False
    def draw(self,screen):
        screen.erase(); height,width=screen.getmaxyx(); content=max(30,min(92,width-6)); left=max(2,(width-content)//2)
        _put(screen,1,left,"● CTA SIGNAL",curses.color_pair(RED)|curses.A_BOLD); _put(screen,1,left+content-26,"CURRENT RAIL DATA",curses.color_pair(GREEN)|curses.A_BOLD)
        _put(screen,3,left,"KNOW BEFORE YOU HEAD DOWNSTAIRS.",curses.A_BOLD)
        _put(screen,5,left,"ASK",curses.color_pair(BLUE)|curses.A_BOLD); _put(screen,5,left+content//3,"CLARIFY",curses.color_pair(AMBER)); _put(screen,5,left+2*content//3,"ANSWER",curses.color_pair(GREEN))
        _put(screen,6,left,"●"+"━"*(content//3-2)+"○"+"━"*(content//3-2)+"○",curses.color_pair(BLUE))
        _put(screen,8,left,"What do you need to know?",curses.A_BOLD)
        question_lines=_question_lines(self.question,content)
        show_cursor=not self.busy and (not self.result or self.result.get("type")!="clarification")
        if show_cursor: question_lines[-1]+="▌"
        for index,line in enumerate(question_lines): _put(screen,10+index,left,line,curses.color_pair(BLUE)|curses.A_BOLD)
        help_row=11+len(question_lines)
        _put(screen,help_row,left,"Enter to ask  ·  Ctrl-R reset  ·  Esc or Ctrl-C exit",curses.color_pair(MUTED))
        row=help_row+3
        if self.busy:
            _put(screen,row,left,"● QUERY SUBMITTED",curses.color_pair(AMBER)|curses.A_BOLD); row+=2
            _put(screen,row,left,"Retrieving current data…",curses.color_pair(AMBER))
        elif self.result:
            kind=self.result["type"]
            if kind=="clarification":
                _put(screen,row,left,"ONE DETAIL NEEDED",curses.color_pair(AMBER)|curses.A_BOLD); row+=2
                for line in _wrapped(self.result["question"],content): _put(screen,row,left,line); row+=1
                row+=1
                for index,option in enumerate(self.result["options"]):
                    selected=index==self.selection; marker="▶ " if selected else "  "; style=(curses.color_pair(AMBER)|curses.A_BOLD) if selected else 0
                    _put_routes(screen,row,left,marker+option["label"],style); row+=2
                _put(screen,row,left,"↑/↓ choose  ·  Enter continue  ·  Backspace edit",curses.color_pair(MUTED))
            else:
                label="CURRENT ANSWER" if kind=="answer" else "COULD NOT ANSWER"; color=GREEN if kind=="answer" else RED
                _put(screen,row,left,label,curses.color_pair(color)|curses.A_BOLD); row+=2
                for line in _wrapped(self.result.get("answer",""),content): _put_routes(screen,row,left,line); row+=1
                sources=self.result.get("sources",[])
                if sources: row+=1; _put_routes(screen,row,left,"Sources: "+"  ·  ".join(sources),curses.color_pair(MUTED))
                row+=2; _put(screen,row,left,"Edit and press Enter, or Ctrl-R for a new question.",curses.color_pair(MUTED))
        if height<24 or width<54: _put(screen,height-2,1,"Tip: enlarge the terminal for the best view.",curses.color_pair(AMBER))
        cursor_row=10+len(question_lines)-1; cursor_column=min(left+len(question_lines[-1])-int(show_cursor),width-2)
        try: screen.move(cursor_row,cursor_column); curses.curs_set(1 if show_cursor else 0)
        except curses.error: pass
        screen.refresh()

def run_tui(service):
    try: curses.wrapper(TUI(service).run)
    except KeyboardInterrupt: pass
