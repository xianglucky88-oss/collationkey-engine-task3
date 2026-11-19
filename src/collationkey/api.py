from . import collation,regex,documentpath,datalog,boolean,quality

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
def quality_call(d):
    """Stateless versioned quality evaluation over caller-supplied relations.

    The request carries schema, an initial row set, versioned batches and the
    rule set directly; there is no importer, file path or remote source. The
    response separates completed, violated and unfinishable judgments.
    """
    registry=quality.QualityRegistry(d['schema'])
    rule_version=registry.register_rules(d['rules'])
    budget=d.get('budget',quality.DEFAULT_BUDGET)
    runs=[]
    for batch in d.get('batches',[]):
        try:
            fact_version=registry.ledger.apply_batch(batch)
        except quality.QualityError as exc:
            runs.append({'status':'rejected_batch','error':str(exc)[:400],
                         'fact_version':registry.ledger.version})
            continue
        assessor=registry.assessor(rule_version,budget=budget)
        try:
            assessor.advance()
        except quality.BudgetExhausted as exc:
            runs.append({'status':'incomplete',
                         'fact_version':fact_version,
                         'rule_version':rule_version,
                         'provisional_violations':exc.partial['provisional_violations'],
                         'work':exc.partial['work'],'budget':budget})
            continue
        runs.append(registry.publish(rule_version,assessor))
    if not runs:
        assessor=registry.assessor(rule_version,budget=budget)
        assessor.advance()
        runs.append(registry.publish(rule_version,assessor))
    return {'rule_version':rule_version,'runs':runs}
OPERATIONS={'collation':collation_call,'regex':lambda d:{'matches':regex.Regex(d['pattern']).fullmatch_chunks(d.get('chunks',[d.get('text','')]))},
 'documentpath':lambda d:{'values':documentpath.select(d['document'],d['path'])},
 'datalog':lambda d:datalog.evaluate(d['facts'],d['rules']), 'boolean':boolean_call,
 'quality':quality_call}
EXAMPLES={
 'collation':{'left':'é','right':'e\u0301','locale':'root','strength':2},
 'regex':{'pattern':'(ab|c)+d?','chunks':['ab','c','d']},
 'documentpath':{'document':{'items':[{'value':1},{'value':None},{}]},'path':'$.items[*].value'},
 'datalog':{'facts':[['edge','a','b'],['edge','b','c']],'rules':[{'head':['path','?a','?b'],'body':[['edge','?a','?b']]},{'head':['path','?a','?c'],'body':[['path','?a','?b'],['edge','?b','?c']]}]},
 'boolean':{'variables':['a','b'],'left':['and','a',['or','a','b']],'right':'a'},
 'quality':{
   'schema':{'tables':[
     {'name':'order','columns':['oid','status'],'key':['oid']},
     {'name':'shipment','columns':['sid','oid'],'key':['sid']},
     {'name':'audit_flag','columns':['sid','reason'],'key':['sid']}]},
   'rules':[
     {'id':'known_order','head':['known_order','?oid'],'body':[['order','?oid','?s']]},
     {'id':'missing_order','head':['violation','?sid'],
      'body':[['shipment','?sid','?oid']],'not':[['known_order','?oid']]},
     {'id':'flagged_shipment','head':['violation','?sid'],'body':[['audit_flag','?sid','?r']]}],
   'batches':[
     [{'op':'add','table':'shipment','id':'s1','values':{'sid':'s1','oid':'o1'}},
      {'op':'add','table':'audit_flag','id':'s1','values':{'sid':'s1','reason':'manual'}}],
     [{'op':'add','table':'order','id':'o1','values':{'oid':'o1','status':'ok'}}],
     [{'op':'delete','table':'audit_flag','id':'s1'}]]},
}
