import json,re

def parse(path):
    if not isinstance(path,str) or not path.startswith('$') or len(path)>4096:raise ValueError("invalid path")
    i=1;steps=[]
    while i<len(path):
        if path.startswith('[*]',i):steps.append(('all',None));i+=3
        elif path[i]=='.':
            m=re.match(r'[A-Za-z_][A-Za-z_0-9]*',path[i+1:])
            if not m:raise ValueError("field name required")
            steps.append(('field',m.group()));i+=1+len(m.group())
        elif path[i]=='[':
            m=re.match(r'\[([0-9]+)\]',path[i:])
            if not m:raise ValueError("nonnegative array index required")
            steps.append(('index',int(m.group(1))));i+=len(m.group())
        else:raise ValueError("unsupported path syntax")
    return steps

def select(document,path,budget=100000):
    values=[document];work=0
    for kind,arg in parse(path):
        out=[]
        for value in values:
            work+=1
            if work>budget:raise ValueError("selection budget exceeded")
            if kind=='field' and isinstance(value,dict) and arg in value:out.append(value[arg])
            elif kind=='index' and isinstance(value,list) and arg<len(value):out.append(value[arg])
            elif kind=='all' and isinstance(value,(list,dict)):out.extend(value.values() if isinstance(value,dict) else value)
        if len(out)>budget:raise ValueError("selection output limit")
        values=out
    return values

def select_json(text,path):
    if len(text)>1_000_000:raise ValueError("document byte limit")
    def unique(pairs):
        obj={}
        for k,v in pairs:
            if k in obj:raise ValueError("duplicate object key")
            obj[k]=v
        return obj
    return select(json.loads(text,object_pairs_hook=unique),path)
