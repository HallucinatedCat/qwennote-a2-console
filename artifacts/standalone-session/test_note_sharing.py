import copy,json,unittest
import note_sharing as share
UUID='note_1';SECRET='ab'*32

def reply(scope=2):
 return {'code':200,'body':{'scope':scope,'roleCode':'role.current','roleSubResourceIds':['current-sub'],
  'enable':{'enable':False,'msg':'not public proof'},'canChangeOwner':False,
  'passwordResponseModel':{'enabled':False},'orgOpenScope':3,'canSearch':True,'canRecommend':False,
  'requirePassword':False,'password':'secret-password','expireAt':123456,'expireDays':7,
  'updateOptions':[{'scope':0,'enable':{'enable':True}},
   {'scope':1,'enable':{'enable':False}},
   {'scope':2,'enable':{'enable':True},'defaultRoleCode':'role.public','defaultSelectedSubResourceIds':['public-sub']}]}}

class SharingTests(unittest.TestCase):
 def test_request_matches_application_contract(self):
  self.assertEqual(share.read_request(UUID),[{'corpId':'','bizType':4,'resourceType':8,'resourceId':UUID,'onlineEdit':False,'sceneCode':0}])
  with self.assertRaises(ValueError):share.read_request('../bad')

 def test_public_projection_hides_credentials_roles_and_grants(self):
  state=share.parse(reply());view=share.public(SECRET,state)
  self.assertEqual(view['access'],'public');self.assertFalse(view['sharing_enabled'])
  self.assertEqual(view['allowed_access'],['private','public']);self.assertFalse(view['password_protected'])
  serialized=json.dumps(view)
  for value in ('secret-password','role.current','role.public','current-sub','public-sub'):self.assertNotIn(value,serialized)
  self.assertEqual(len(view['revision']),64)

 def test_private_save_preserves_flags_and_uses_current_role_fallback(self):
  state=share.parse(reply());request=share.save_request(UUID,state,'private');body=request[0]
  self.assertEqual(body['scope'],0);self.assertEqual(body['roleCode'],'role.current');self.assertEqual(body['roleSubResourceIds'],[])
  for key in ('orgOpenScope','canSearch','canRecommend','requirePassword','password','expireAt','expireDays'):
   self.assertEqual(body[key],reply()['body'][key])
  self.assertEqual(body['resourceId'],UUID)

 def test_public_save_uses_target_defaults(self):
  state=share.parse(reply(0));request=share.save_request(UUID,state,'public');body=request[0]
  self.assertEqual((body['scope'],body['roleCode'],body['roleSubResourceIds']),(2,'role.public',['public-sub']))

 def test_disabled_or_organization_targets_are_rejected(self):
  state=share.parse(reply());state['options'][0]['enabled']=False
  with self.assertRaises(ValueError):share.save_request(UUID,state,'private')
  with self.assertRaises(ValueError):share.save_request(UUID,state,'organization')

 def test_readback_requires_target_and_exact_preservation(self):
  original=share.parse(reply());request=share.save_request(UUID,original,'private');observed=copy.deepcopy(original)
  observed.update(scope=0,role_code='role.current',role_subresource_ids=[])
  self.assertTrue(share.verified(original,observed,request))
  normalized=copy.deepcopy(observed);normalized['role_code']=None
  self.assertTrue(share.verified(original,normalized,request))
  serialized=json.loads(json.dumps(original));pending={'original':serialized,'request':request}
  self.assertTrue(share.resolved(original,pending))
  self.assertTrue(share.resolved(normalized,pending))
  for field,value in [('scope',2),('role_code','other'),('role_subresource_ids',['other']),('can_search',False),('password','changed'),('sharing_enabled',True)]:
   bad=copy.deepcopy(observed);bad[field]=value;self.assertFalse(share.verified(original,bad,request),field)

 def test_revision_and_pending_resolution_detect_changes(self):
  state=share.parse(reply());pending={'original':state,'request':share.save_request(UUID,state,'private')}
  self.assertTrue(share.resolved(copy.deepcopy(state),pending))
  changed=copy.deepcopy(state);changed['password']='different';self.assertFalse(share.resolved(changed,pending))
  self.assertNotEqual(share.revision(SECRET,state),share.revision('cd'*32,state))

 def test_invalid_shapes_are_rejected(self):
  for mutate in [lambda b:b.update(scope=9),lambda b:b.update(scope=True),lambda b:b.update(roleCode=3),
                 lambda b:b.update(updateOptions=[{'scope':0},{'scope':0}]),
                 lambda b:b.update(roleSubResourceIds=[3]),lambda b:b.update(canSearch='yes')]:
   value=reply();mutate(value['body'])
   with self.assertRaises(ValueError):share.parse(value)
  for value in (None,{}, {'code':500,'body':{}},{'code':200,'body':[] }):
   with self.assertRaises(ValueError):share.parse(value)

 def test_acknowledgement_is_strict(self):
  self.assertTrue(share.acknowledged({'code':200,'body':{}}));self.assertTrue(share.acknowledged({'code':200,'body':None}))
  for value in ({'code':500,'body':{}},{'code':200,'body':{'code':123}},{'code':200,'body':'ok'}):self.assertFalse(share.acknowledged(value))
 def test_share_url_uses_application_detail_field_and_validates_https(self):
  value={'code':200,'body':{'data':{'uuid':UUID,'taskId':1,'shareUrl':'https://minutes.qwennote.cn/share/example?ticket=opaque'}}}
  self.assertEqual(share.share_url(value,UUID),'https://minutes.qwennote.cn/share/example?ticket=opaque')
  value['body']['data']['shareUrl']='';self.assertIsNone(share.share_url(value,UUID))
  for url in ('javascript:alert(1)','http://example.invalid/share','https://user:pass@example.invalid/share','https://example.invalid/\nInjected'):
   value['body']['data']['shareUrl']=url
   with self.assertRaises(ValueError):share.share_url(value,UUID)
  value['body']['data']['uuid']='other'
  with self.assertRaises(ValueError):share.share_url(value,UUID)

if __name__=='__main__':unittest.main()
