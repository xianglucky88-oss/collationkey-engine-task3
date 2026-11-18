from . import collation,regex,documentpath,datalog,boolean

def collation_call(d):
    with collation.Collator(d.get('locale','root'),d.get('strength',2)) as c:
        return {'comparison':c.compare(d['left'],d['right']),'left_key':c.key(d['left']),'right_key':c.key(d['right'])}
def boolean_call(d):
    b=boolean.BDD(d['variables'])
    def compile_expr(e):
        if isinstance(e,bool):return int(e)
        if isinstance(e,str):return b.variable(e)
        if e[0]=='not':return b.negate(compile_expr(e[1]))
        return b.apply(e[0],compile_expr(e[1]),compile_expr(e[2]))
    a,c=compile_expr(d['left']),compile_expr(d['right']);different=b.apply('xor',a,c)
    return {'equivalent':different==0,'counterexample':b.witness(different),'node_count':len(b.nodes)}
OPERATIONS={'collation':collation_call,'regex':lambda d:{'matches':regex.Regex(d['pattern']).fullmatch_chunks(d.get('chunks',[d.get('text','')]))},
 'documentpath':lambda d:{'values':documentpath.select(d['document'],d['path'])},
 'datalog':lambda d:datalog.evaluate(d['facts'],d['rules']), 'boolean':boolean_call}
EXAMPLES={
 'collation':{'left':'é','right':'e\u0301','locale':'root','strength':2},
 'regex':{'pattern':'(ab|c)+d?','chunks':['ab','c','d']},
 'documentpath':{'document':{'items':[{'value':1},{'value':None},{}]},'path':'$.items[*].value'},
 'datalog':{'facts':[['edge','a','b'],['edge','b','c']],'rules':[{'head':['path','?a','?b'],'body':[['edge','?a','?b']]},{'head':['path','?a','?c'],'body':[['path','?a','?b'],['edge','?b','?c']]}]},
 'boolean':{'variables':['a','b'],'left':['and','a',['or','a','b']],'right':'a'},
}
