class BDD:
    def __init__(self,variables,max_nodes=10000):
        if len(variables)>24 or len(variables)!=len(set(variables)):raise ValueError("invalid variable order")
        self.variables=tuple(variables);self.order={v:i for i,v in enumerate(variables)};self.nodes={0:None,1:None};self.unique={};self.cache={};self.limit=max_nodes
    def node(self,var,low,high):
        if low==high:return low
        key=(var,low,high)
        if key not in self.unique:
            if len(self.nodes)>=self.limit:raise ValueError("decision diagram node limit")
            i=len(self.nodes);self.unique[key]=i;self.nodes[i]=key
        return self.unique[key]
    def variable(self,name):
        if name not in self.order:raise ValueError("unknown variable")
        return self.node(self.order[name],0,1)
    def apply(self,op,a,b):
        if op not in {'and','or','xor'} or a not in self.nodes or b not in self.nodes:raise ValueError("invalid boolean operation")
        key=(op,a,b)
        if key in self.cache:return self.cache[key]
        if a<2 and b<2:return int(a and b) if op=='and' else int(bool(a or b)) if op=='or' else a^b
        va=self.nodes[a][0] if a>=2 else len(self.variables);vb=self.nodes[b][0] if b>=2 else len(self.variables);var=min(va,vb)
        al,ah=self.nodes[a][1:] if va==var else (a,a);bl,bh=self.nodes[b][1:] if vb==var else (b,b)
        result=self.node(var,self.apply(op,al,bl),self.apply(op,ah,bh));self.cache[key]=result;return result
    def negate(self,node):return self.apply('xor',node,1)
    def exists(self,node,name):
        if name not in self.order:raise ValueError("unknown variable")
        target=self.order[name];memo={}
        def visit(n):
            if n<2:return n
            if n in memo:return memo[n]
            v,lo,hi=self.nodes[n];a,b=visit(lo),visit(hi)
            memo[n]=self.apply('or',a,b) if v==target else self.node(v,a,b);return memo[n]
        return visit(node)
    def evaluate(self,node,assignment):
        if node not in self.nodes:raise ValueError("foreign node")
        while node>=2:
            v,lo,hi=self.nodes[node];name=self.variables[v]
            if name not in assignment or type(assignment[name]) is not bool:raise ValueError("complete boolean assignment required")
            node=hi if assignment[name] else lo
        return bool(node)
    def witness(self,node):
        if node==0:return None
        if node not in self.nodes:raise ValueError("foreign node")
        result={name:False for name in self.variables}
        while node>=2:
            v,lo,hi=self.nodes[node];choose=lo==0;result[self.variables[v]]=choose;node=hi if choose else lo
        return result
