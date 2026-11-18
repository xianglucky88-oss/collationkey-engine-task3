class Regex:
    def __init__(self,pattern):
        if not isinstance(pattern,str) or len(pattern)>128:raise ValueError("pattern size limit")
        self.pattern=pattern;self.pos=0;self.edges={};self.states=0
        self.start,self.end=self.alt()
        if self.pos!=len(pattern):raise ValueError("unexpected pattern token")
    def state(self):i=self.states;self.states+=1;self.edges[i]=[];return i
    def arc(self,a,label,b):self.edges[a].append((label,b))
    def alt(self):
        a,b=self.seq()
        while self.pos<len(self.pattern) and self.pattern[self.pos]=='|':
            self.pos+=1;x,y=self.seq();s,t=self.state(),self.state();self.arc(s,None,a);self.arc(s,None,x);self.arc(b,None,t);self.arc(y,None,t);a,b=s,t
        return a,b
    def seq(self):
        s=self.state();last=s
        while self.pos<len(self.pattern) and self.pattern[self.pos] not in ')|':
            a,b=self.atom();self.arc(last,None,a);last=b
        return s,last
    def atom(self):
        ch=self.pattern[self.pos];self.pos+=1
        if ch=='(':
            a,b=self.alt()
            if self.pos>=len(self.pattern) or self.pattern[self.pos]!=')':raise ValueError("unclosed group")
            self.pos+=1
        else:
            if ch=='\\':
                if self.pos>=len(self.pattern):raise ValueError("unfinished escape")
                ch=self.pattern[self.pos];self.pos+=1
                if ch.isdigit():raise ValueError("backreferences unsupported")
                label=('literal',ch)
            elif ch=='.':label=('any',None)
            elif ch in '*+?[]{}^$':raise ValueError("unsupported pattern token")
            else:label=('literal',ch)
            a,b=self.state(),self.state();self.arc(a,label,b)
        if self.pos<len(self.pattern) and self.pattern[self.pos] in '*+?':
            op=self.pattern[self.pos];self.pos+=1;s,t=self.state(),self.state();self.arc(s,None,a);self.arc(b,None,t)
            if op in '*?':self.arc(s,None,t)
            if op in '*+':self.arc(b,None,a)
            a,b=s,t
        return a,b
    def closure(self,states):
        seen=set(states);stack=list(states)
        while stack:
            for label,target in self.edges[stack.pop()]:
                if label is None and target not in seen:seen.add(target);stack.append(target)
        return seen
    def fullmatch(self,text,budget=1_000_000):return self.fullmatch_chunks([text],budget)
    def fullmatch_chunks(self,chunks,budget=1_000_000):
        states=self.closure([self.start]);work=0
        for chunk in chunks:
            for char in chunk:
                nxt=set()
                for state in states:
                    for label,target in self.edges[state]:
                        work+=1
                        if work>budget:raise ValueError("matching budget exceeded")
                        if label is not None and ((label[0]=='any' and char!='\n') or label==('literal',char)):nxt.add(target)
                states=self.closure(nxt)
        return self.end in states
