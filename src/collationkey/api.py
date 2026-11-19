import json
from . import collation,regex,documentpath,documentstream,datalog,boolean

def documentconvert_call(d):
    if 'chunks' in d:
        chunks=d['chunks']
        if not isinstance(chunks,list):raise ValueError('chunks must be a list of strings')
    else:
        doc=d.get('document','')
        chunks=[doc if isinstance(doc,str) else json.dumps(doc,ensure_ascii=False,allow_nan=False)]
    return documentstream.convert(chunks,d)

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
 'documentconvert':documentconvert_call,
 'datalog':lambda d:datalog.evaluate(d['facts'],d['rules']), 'boolean':boolean_call}
EXAMPLES={
 'collation':{'left':'é','right':'e\u0301','locale':'root','strength':2},
 'regex':{'pattern':'(ab|c)+d?','chunks':['ab','c','d']},
 'documentpath':{'document':{'items':[{'value':1},{'value':None},{}]},'path':'$.items[*].value'},
 'documentconvert':{'arrays':'zip','fields':[{'path':'$.items[*].sku','field':'sku','type':'string'},{'path':'$.items[*].qty','field':'qty','type':'integer'},{'path':'$.batch','field':'batch','type':'string'}],'document':{'items':[{'sku':'a','qty':2},{'sku':'b','qty':3}],'batch':'b1'}},
 'datalog':{'facts':[['edge','a','b'],['edge','b','c']],'rules':[{'head':['path','?a','?b'],'body':[['edge','?a','?b']]},{'head':['path','?a','?c'],'body':[['path','?a','?b'],['edge','?b','?c']]}]},
 'boolean':{'variables':['a','b'],'left':['and','a',['or','a','b']],'right':'a'},
}
