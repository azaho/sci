"""Explicit regression cases for the current draft and gaps found in both corpora."""
import base64
import copy


def generate_updates(pub, emit, bundle, key, jwk, encode):
    meta = {'title': 'Current draft fixture', 'abstract': 'Synthetic test data.'}
    pub('readme-whitespace-only', '2,4.2', 'README must be nonblank.',
        outcome='invalid', readme=b' \t\r\n')
    pub('empty-license', '2,4.2', 'Only README is required to be nonblank; empty LICENSE is valid but not baseline.',
        baseline=False, license_bytes=b'')
    # Fixed range boundaries, including every noncharacter plane ending.
    allowed = [0x20, 0x7e, 0xa0, 0xad, 0x378, 0x61c, 0x200b, 0x200c, 0x200d,
               0x2028, 0x2029, 0x202e, 0x2066, 0xd7ff, 0xe000, 0xfdcf, 0xfdf0,
               0xfeff, 0xfffd, 0x10000, 0xe0001, 0xe007f, 0xe0100, 0xf0000, 0x10fffd]
    forbidden = [cp for cp in range(0x20) if cp not in (9, 10, 13)] + list(range(0x7f, 0xa0))
    forbidden += [0xfdd0, 0xfdef] + [plane * 0x10000 + end for plane in range(17) for end in (0xfffe, 0xffff)]
    for cp in allowed + forbidden:
        valid = cp in allowed
        pub(f'assignable-u{cp:04x}', '4.2', 'RFC 9839 range membership, independent of runtime Unicode properties.',
            outcome='valid' if valid else 'invalid', metadata={**meta, 'title': 'x'+chr(cp)+'y'})
    # Same policy in every text location; non-ASCII whitespace alone counts as
    # nonblank under SCI's explicitly enumerated SPACE/TAB/LF/CR definition.
    for field in ('title', 'abstract', 'keywords', 'categories', 'display', 'citations'):
        for valid, char in ((True, '\ue000'), (False, '\u0085')):
            m=copy.deepcopy(meta)
            if field in ('title','abstract'):m[field]='x'+char
            elif field=='keywords':m[field]=['x'+char]
            elif field=='categories':m[field]=[{'scheme':'custom','code':'x'+char}]
            elif field=='display':m[field]=[{'path':'files/a','caption':'x'+char}]
            else:m[field]=[{'target':'https://example.org','reason':'x'+char}]
            pub(f'text-field-{field}-{valid}', '4.2', 'Apply Assignables to each decoded text field.',
                outcome='valid' if valid else 'invalid', metadata=m, files={'files/a':b'x'})
    pub('title-nbsp-only', '4.2', 'NBSP is permitted and is not SPACE/TAB/LF/CR.', metadata={**meta,'title':'\u00a0'})
    pub('readme-joiner-only', '2,4.2', 'Nonblank is the explicit code-point rule, not a visibility test.', readme='\u200d'.encode())
    pub('metadata-escaped-bidi', '4.2,7', 'JSON escape spelling does not change Assignables membership.',
        files={'sci.json':b'{"title":"x\\u202ey","abstract":"a"}'})
    pub('metadata-escaped-c1', '4.2,7', 'Escaped C1 controls remain prohibited.', outcome='invalid',
        files={'sci.json':b'{"title":"x\\u0085y","abstract":"a"}'})
    for cp in (0xd800,0xdfff):
        pub(f'metadata-surrogate-{cp:x}', '7', 'An unpaired surrogate escape is invalid.', outcome='invalid',
            files={'sci.json': ('{"title":"\\u%04x","abstract":"a"}'%cp).encode()})
    for label,data in [('surrogate',b'\xed\xa0\x80'),('above-max',b'\xf4\x90\x80\x80'),('truncated',b'\xf0\x9f\x98')]:
        pub('readme-utf8-'+label,'2,4.2','Ill-formed UTF-8 must be rejected, not replaced.',outcome='invalid',readme=data)
    pub('manifest-negative-zero-size','3,7','-0 parses as zero but is not digits-only byte-count spelling.',
        outcome='invalid',files={'files/empty':b''},manifest_bytes=lambda b:b.replace(b'"bytes":0}',b'"bytes":-0}'))
    for label,transform in [('hex',lambda s:s.hex().encode()),('base64',base64.b64encode),('newline',lambda s:s+b'\n')]:
        pub('signature-text-'+label,'3,5.1','Signature file must be exactly the raw 64 bytes.',outcome='invalid',signature_change=transform)
    pub('manifest-cross-type','5.1,6','A supported claim schema is the wrong record type, not an unknown schema.',
        outcome='invalid',manifest_change=lambda m:m.update(schema='sci-key-claim/0.1'))
    for label,change in [('missing',lambda m:m.pop('schema')),('null',lambda m:m.update(schema=None)),('array',lambda m:m.update(files=[]))]:
        pub('manifest-shape-'+label,'3','Malformed required fields are invalid.',outcome='invalid',manifest_change=change)

    index={'schema':'sci-index/0.1','publication_lookup_url':'https://index.example/p/{id}',
           'revocation_lookup_url':'https://index.example/k/{x}/{file}',
           'timestamp_lookup_url':'https://index.example/t/{id}'}
    for name,obj,valid in [
        ('index-submit-url',{**index,'submit_url':'https://index.example/submit'},True),
        ('index-legacy-lookup-only',{**{k:v for k,v in index.items() if k!='publication_lookup_url'},'lookup_url':index['publication_lookup_url']},False),
        ('index-legacy-extension',{**index,'lookup_url':index['publication_lookup_url']},True),
        ('index-submit-relative',{**index,'submit_url':'/submit'},False),
        ('index-submit-template',{**index,'submit_url':'https://index.example/{id}'},False),
        ('index-policy-relative',{**index,'policy_url':'/policy'},False),
        ('index-template-unclosed',{**index,'publication_lookup_url':'https://index.example/{id'},False),
    ]:
        emit(name,'discovery','8.1','Current descriptor fields and permitted expressions.',
             {'descriptor.json':encode(obj)},{'outcome':'valid' if valid else 'invalid'},{'descriptor_kind':'index'})
    request={'id':'a'*64,'descriptors':['https://archive.example/sci/archive.json']}
    samples=[('minimal',request,True),('account',{**request,'account':{'type':'domain','id':'example.org'}},True),
             ('multiple',{**request,'descriptors':request['descriptors']+['https://mirror.example/archive.json']},True)]
    for label,field,value in [('id-prefix','id','sha256:'+'a'*64),('id-uppercase','id','A'*64),('id-short','id','a'*63),
                              ('descriptors-empty','descriptors',[]),('descriptors-duplicate','descriptors',request['descriptors']*2),
                              ('descriptors-relative','descriptors',['/archive.json']),('descriptors-type','descriptors','https://example.org'),
                              ('descriptors-item-type','descriptors',[1]),('extra-field','publication',True),
                              ('account-extra','account',{'type':'domain','id':'example.org','verified':True}),
                              ('account-type','account',{'type':'domain','id':1})]:
        samples.append((label,{**request,field:value},False))
    samples.append(('missing-id',{'descriptors':request['descriptors']},False))
    for label,obj,valid in samples:
        emit('submit-request-'+label,'submission','8.1.4','Publication submission request shape; account is an unsigned hint.',
             {'request.json':encode(obj)},{'outcome':'valid' if valid else 'invalid'},{'phase':'request'})
    emit('submit-request-duplicate-key','submission','7,8.1.4','Duplicate decoded request keys are forbidden.',
         {'request.json':b'{"id":"'+b'a'*64+b'","i\\u0064":"'+b'a'*64+b'","descriptors":["https://example.org/a"]}'},
         {'outcome':'invalid'},{'phase':'request'})
    for label,status,obj,valid in [
        ('registered',200,{'id':'a'*64},True),('created',201,{'id':'a'*64,'service_extra':True},True),
        ('wrong-id',200,{'id':'b'*64},False),('missing-id',201,{},False),('wrong-status',202,{'id':'a'*64},False),
        ('error',422,{'error':{'code':'invalid_bundle','message':'Failed validation','retryable':False}},True),
        ('retryable-error',503,{'error':{'code':'unavailable','message':'Try later','retryable':True}},True),
        ('error-retryable-type',503,{'error':{'code':'unavailable','message':'Try later','retryable':'yes'}},False),
        ('error-missing-code',422,{'error':{'message':'Bad','retryable':False}},False),
    ]:
        emit('submit-response-'+label,'submission','8.1.4','Protocol success registers a location; it does not promise catalog admission.',
             {'response.json':encode(obj)},{'outcome':'valid' if valid else 'invalid'},
             {'phase':'response','status':status,'content_type':'application/json','requested_id':'a'*64})
    for name,events,expected in [
        ('submission-retry-idempotent',['submit_location','lose_success_response','repeat_same_submission'],{'location_registrations':1}),
        ('submission-mirror-registration',['submit_location','submit_second_location_same_id'],{'publication_ids':1,'location_registrations':2}),
        ('submission-unsigned-account-hint',['submit_location_with_unverified_account_hint'],{'association_verified':False}),
    ]:
        emit(name,'scenario','5.2,8.1.4','Ordered protocol integration scenario; execute through an implementation adapter.',
             {'events.json':encode({'events':events})},expected,{'publication':'minimal-baseline','key':jwk['x']})
