import itertools,re,json
import pytest
from collationkey import collation,regex,documentpath,datalog,boolean

def test_real_icu_canonical_equivalence_and_key_profile():
    with collation.Collator('root',2) as c:
        assert c.compare('é','e\u0301')==0
        values=['z','a','é','e','Z'];ordered=sorted(values,key=lambda x:bytes.fromhex(c.key(x)['key']))
        for a,b in zip(ordered,ordered[1:]):assert c.compare(a,b)<=0
        a=c.key('a');b=c.key('b');assert c.compare_keys(a,b)<0
    with collation.Collator('root',0) as c:b=c.key('b')
    with pytest.raises(ValueError):collation.Collator.compare_keys(a,b)
@pytest.mark.parametrize('pattern',['a*','(ab|c)+d?','a.b','a|','(a?)*','ab+c'])
def test_regex_supported_subset_against_independent_engine(pattern):
    ours=regex.Regex(pattern)
    for n in range(4):
        for chars in itertools.product('abc',repeat=n):
            text=''.join(chars);expected=re.fullmatch(pattern,text) is not None
            assert ours.fullmatch(text)==expected
            assert ours.fullmatch_chunks([text[:1],text[1:]])==expected
    with pytest.raises(ValueError):regex.Regex('[a-z]')
def test_document_path_null_missing_and_duplicate_keys():
    obj={'x':[{'v':None},{'v':3},{}]}
    assert documentpath.select(obj,'$.x[*].v')==[None,3]
    assert documentpath.select(obj,'$.x[99]')==[]
    with pytest.raises(ValueError):documentpath.select_json('{"a":1,"a":2}','$.a')
    with pytest.raises(ValueError):documentpath.select(obj,'$..v')
def test_recursive_closure_and_stratified_negation():
    facts=[['edge','a','b'],['edge','b','c'],['blocked','c']]
    rules=[{'head':['path','?a','?b'],'body':[['edge','?a','?b']]},
           {'head':['path','?a','?c'],'body':[['path','?a','?b'],['edge','?b','?c']]},
           {'head':['safe','?a','?b'],'body':[['path','?a','?b']],'not':[['blocked','?b']]}]
    result=datalog.evaluate(facts,rules)
    assert result['path']==[['a','b'],['a','c'],['b','c']]
    assert result['safe']==[['a','b']]
    assert datalog.evaluate([facts[0]],rules)['path']==[['a','b']]
    with pytest.raises(ValueError):datalog.evaluate([], [{'head':['p','?x'],'body':[]}])
    with pytest.raises(ValueError):datalog.evaluate([['domain','a']],[{'head':['p','?x'],'body':[['domain','?x']],'not':[['p','?x']]}])
def test_boolean_algebra_quantification_and_witness():
    b=boolean.BDD(['a','b','c']);a,c=b.variable('a'),b.variable('b')
    expr=b.apply('and',a,b.apply('or',a,c));assert expr==a
    both=b.apply('and',a,c);assert b.exists(both,'b')==a
    unequal=b.apply('xor',a,c);w=b.witness(unequal);assert b.evaluate(unequal,w)
    for av,bv,cv in itertools.product([False,True],repeat=3):
        env={'a':av,'b':bv,'c':cv};assert b.evaluate(both,env)==(av and bv)
    with pytest.raises(ValueError):boolean.BDD(['a'],max_nodes=2).variable('a')
