"""Read-only application membership/traffic contracts as the official app uses them.

Unknown counters remain null. Vendor display text and quota dates are preserved;
benefit advertising, free access and daily allowances do not imply unlimited use.
"""
import math
import re

PREFIX='/r/Adaptor/'
MINUTE_TAGS={'org_month_quota_account','org_day_quota_account','personal_day_quota_account',
             'personal_month_quota_account','hardware_month_quota_account','flash_minute_quota'}
BENEFITS={'speech-to-text':'transcription','minutes_image_summary':'image_summary',
          'minutes_ask_ai':'ai_qa','device_cloud_storage':'cloud_storage','data_usage':'device_data'}
STATUSES={'0':'personal_free','1':'personal_paid','2':'personal_expired','3':'enterprise_free','4':'enterprise_paid'}
SCOPES={'0':'personal','1':'enterprise'}


def requests(config,corp_id=None):
    account=str(config['account_id']);device=str(config['target_device_id']);kind=int(config['account_type'])
    if not account or not device.isdecimal() or not 0<int(device)<=9007199254740991:
        raise ValueError('Invalid quota target')
    if corp_id is not None and (not isinstance(corp_id,str) or not corp_id.isdecimal() or not 0<=int(corp_id)<2**63):
        raise ValueError('Invalid quota organization')
    organization={} if corp_id is None else {'corpId':corp_id}
    return [
        ('account',PREFIX+'MoziBmsStatisticsI/getLicenceDashboard',[{'business':'flash_minute','boardType':-1,**organization}]),
        ('device',PREFIX+'MoziBmsStatisticsI/getDeviceLicenceDashboard',[{'business':'flash_minute','accountType':str(kind),'accountId':account,'deviceId':device,**organization}]),
        ('detail',PREFIX+'MoziBmsStatisticsI/getLicenceDetail',[{'business':'flash_minute',**organization}]),
        ('traffic',PREFIX+'DeviceTrafficI/queryDeviceTrafficUsage',[{'accountType':kind,'accountId':account,'targetDeviceId':int(device)}]),
    ]


def text(value,limit=2048):
    if value is None:return ''
    if not isinstance(value,str) or len(value)>limit or any(ord(c)<32 and c not in '\n\t' for c in value):
        raise ValueError('Invalid quota display text')
    return value


def number(value):
    if type(value) not in (int,float,str):return None
    if isinstance(value,str) and (len(value)>64 or not re.fullmatch(r'\d+(?:\.\d+)?',value)):return None
    try:n=float(value)
    except (ValueError,OverflowError):return None
    if not math.isfinite(n) or not 0<=n<=9007199254740991:return None
    return int(n) if n.is_integer() else n


def rows(value,limit):
    if not isinstance(value,list) or len(value)>limit or any(not isinstance(v,dict) for v in value):
        raise ValueError('Invalid quota list')
    return value


def mapping(value):
    if not isinstance(value,dict):raise ValueError('Invalid quota object')
    return value


def counters(used,total,unit=None):
    used=number(used);total=number(total)
    return {'used':used,'total':total,'remaining':max(0,total-used) if used is not None and total is not None else None,'unit':unit}


def body(reply):
    if not isinstance(reply,dict) or type(reply.get('code')) is not int or reply['code']!=200:
        raise ValueError('Quota read rejected')
    result=mapping(reply.get('body'))
    if 'code' in result and (type(result['code']) is not int or result['code'] not in (0,200)):
        raise ValueError('Quota read rejected')
    return result


def dashboard(reply):
    result=[]
    for board in rows(body(reply).get('licenceBoardList'),8):
        resource=mapping(board.get('resourceBoard'));extra=mapping(resource.get('extraInfo',{}));biz=extra.get('bizType')
        usages=[]
        for index,item in enumerate(rows(board.get('functionBoardList',[]),32)):
            info=mapping(item.get('extraInfo',{}));minutes=index==0 and biz in MINUTE_TAGS
            usages.append({'category':'transcription' if minutes else 'other','label':text(item.get('functionNameContent'),256),
                'display':text(item.get('functionValueContent')),'tip':text(item.get('functionValueTips')),
                **counters(info.get('used'),info.get('total'),'minutes' if minutes else None)})
        benefits=[]
        for item in rows(board.get('adBoardList',[]),32):
            category=BENEFITS.get(item.get('adKey'),'other')
            if item.get('adType')!=1:continue
            display=text(item.get('adDetailContent'));match=re.fullmatch(r'(\d+(?:\.\d+)?)\s*(篇|次|G|GB|MB)\s*剩余',display.strip())
            benefits.append({'category':category,'vendor_key':text(item.get('adKey'),128),'label':text(item.get('adTitleContent'),256),'display':display,
                'remaining':number(match[1]) if match else None,'unit':match[2] if match else None,
                'limited_time_free':item.get('limitFree') is True})
        result.append({'scope':SCOPES.get(board.get('boardType'),'unknown'),'membership':STATUSES.get(board.get('boardStatus'),'unknown'),
            'title':text(resource.get('resourceNameContent'),256),'description':text(resource.get('functionRangeContent')),
            'tip':text(resource.get('functionRangeTip')),'usages':usages,'benefits':benefits,
            'text_summary_counter_reported':False})
    return {'boards':result}


def detail(reply):
    result=[]
    for item in rows(body(reply).get('list'),100):
        tag=item.get('resourceTag');minute=tag in MINUTE_TAGS
        result.append({'name':text(item.get('resourceName'),256),
            'unit':'minutes' if minute else None,**counters(item.get('resourceUsed'),item.get('resourceQuota'),'minutes' if minute else None),
            'daily_allowance':tag in {'personal_day_quota_account','org_day_quota_account'},
            'starts':text(item.get('startTime'),64),'expires':text(item.get('endTime'),64),'resets':text(item.get('updateTime'),64)})
    return {'items':result}


def traffic(reply):
    data=body(reply)
    if not any(k in data for k in ('quota','remaining','used')):raise ValueError('Invalid traffic reply')
    return {'used':number(data.get('used')),'total':number(data.get('quota')),'remaining':number(data.get('remaining')),
        'unit':'KB','scale':1024,'billing_cycle':None,'tip':text(data.get('tips')),
        'daily':[{'date':text(item.get('updateTime'),64),'used':number(item.get('used'))}
                 for item in rows(data.get('dailyUsageList',[]),366)]}


PARSERS={'account':dashboard,'device':dashboard,'detail':detail,'traffic':traffic}
