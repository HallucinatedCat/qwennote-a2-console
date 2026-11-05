"""Whether a note is still being recorded, transcribed or summarized, from minutesDetailV2 data.

As the official app does it: PortalTaskModel.status is decoded with
businessType; status 15 (hardware pre-create) reads hardwareFileInfoModel.fileStatus;
summaryExtractStatus 0/1 = generating, 2 = done, 3 = failed. queryMeetingBrief returns
extractStatus 1 (or an empty body) while the summary is still being generated.
Pushes 86001/86003 on doc.transcribe announce transcribe_completed, text_summary_generated
and list refreshes.
"""

NOTE_EVENTS=(86001,86003)


def transcription(data):
    """recording | transcribing | failed | completed | unknown"""
    if not isinstance(data,dict):return 'unknown'
    status=data.get('status');kind=data.get('businessType')
    if type(status) is not int:return 'unknown'
    if kind in (0,1):return 'recording' if status==0 else 'completed'
    if kind==2:
        if status==15:
            info=data.get('hardwareFileInfoModel') if isinstance(data.get('hardwareFileInfoModel'),dict) else {}
            return 'failed' if info.get('fileStatus') in ('noStorage','noCommerceAbility') else 'transcribing'
        return {13:'failed',14:'completed'}.get(status,'transcribing')
    if kind==5 and status in (0,1,2):return 'recording'
    if status in (5,6,10,11,12,15,20,21,22,23,24):return 'transcribing'
    if status in (13,25):return 'failed'
    return 'completed'


def summary(data):
    """generating | failed | completed | none, once transcription is complete."""
    value=data.get('summaryExtractStatus') if isinstance(data,dict) else None
    return {0:'generating',1:'generating',2:'completed',3:'failed'}.get(value,'none')


def processing(data):
    """The state a note view should show instead of content, or None when content is final."""
    state=transcription(data)
    if state in ('recording','transcribing','failed'):return state
    return 'generating' if summary(data)=='generating' else None


def brief_generating(reply):
    """Only an explicit extractStatus 1. An empty reply also comes back for a note that will
    never get a summary (a few seconds of silence), so it must not mean "still generating"."""
    body=reply.get('body') if isinstance(reply,dict) else None
    return isinstance(body,dict) and body.get('extractStatus')==1


def summary_empty(reply):
    """True when a finished note has no summary text at all."""
    body=reply.get('body') if isinstance(reply,dict) else None
    full=body.get('fullTextSummary') if isinstance(body,dict) else None
    return not isinstance(full,dict) or not str(full.get('value') or '').strip()


def event(payload):
    """(note uuid or None, event name) from a 86001/86003 payload."""
    if not isinstance(payload,dict):return None,None
    uuid=payload.get('uuid') if isinstance(payload.get('uuid'),str) and len(payload['uuid'])<=128 else None
    name=payload.get('eventName') if isinstance(payload.get('eventName'),str) and len(payload['eventName'])<=64 else payload.get('nameSpace')
    return uuid,name if isinstance(name,str) and len(name)<=64 else None
