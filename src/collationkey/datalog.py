from collections import defaultdict

def isvar(x):return isinstance(x,str) and x.startswith('?')
def variables(atom):return {x for x in atom[1:] if isvar(x)}
def evaluate(facts,rules,budget=100000):
    if len(facts)>10000 or len(rules)>128:raise ValueError("program size limit")
    relations=defaultdict(set);arity={};predicates=set()
    def check(atom):
        if not isinstance(atom,(list,tuple)) or not atom or not isinstance(atom[0],str):raise ValueError("invalid atom")
        name=atom[0];n=len(atom)-1
        if name in arity and arity[name]!=n:raise ValueError("arity mismatch")
        if any(not isinstance(x,(str,int,bool)) for x in atom):raise ValueError("scalar terms required")
        arity[name]=n;predicates.add(name)
    for f in facts:
        check(f)
        if variables(f):raise ValueError("facts must be ground")
        relations[f[0]].add(tuple(f[1:]))
    for r in rules:
        check(r['head'])
        positives=r.get('body',[]);negative=r.get('not',[])
        for a in positives+negative:check(a)
        bound=set().union(set(),*(variables(a) for a in positives))
        if not variables(r['head'])<=bound or any(not variables(a)<=bound for a in negative):raise ValueError("unsafe variable")
    strata={p:0 for p in predicates}
    for iteration in range(len(predicates)+1):
        changed=False
        for r in rules:
            head=r['head'][0]
            for a in r.get('body',[])+r.get('not',[]):
                required=strata[a[0]]+(1 if a in r.get('not',[]) else 0)
                if strata[head]<required:strata[head]=required;changed=True
        if not changed:break
    else:raise ValueError("unstratified negation")
    work=0
    for level in sorted(set(strata.values())):
        relevant=[r for r in rules if strata[r['head'][0]]==level]
        changed=True
        while changed:
            changed=False
            for r in relevant:
                envs=[{}]
                for atom in r.get('body',[]):
                    expanded=[]
                    for env in envs:
                        for row in list(relations[atom[0]]):
                            work+=1
                            if work>budget:raise ValueError("rule work budget exceeded")
                            e=dict(env);ok=True
                            for term,value in zip(atom[1:],row):
                                if isvar(term):
                                    if term in e and e[term]!=value:ok=False;break
                                    e[term]=value
                                elif term!=value:ok=False;break
                            if ok:expanded.append(e)
                    envs=expanded
                for env in envs:
                    def ground(atom):return tuple(env[t] if isvar(t) else t for t in atom[1:])
                    if any(ground(a) in relations[a[0]] for a in r.get('not',[])):continue
                    row=ground(r['head']);name=r['head'][0]
                    if row not in relations[name]:relations[name].add(row);changed=True;work+=1
                    if work>budget:raise ValueError("rule work budget exceeded")
    return {p:sorted([list(v) for v in rows],key=repr) for p,rows in sorted(relations.items())}
