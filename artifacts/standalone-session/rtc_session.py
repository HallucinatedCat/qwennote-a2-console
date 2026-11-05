"""Bounded RTC session lifecycle shared by the host UI and isolated CLI."""
import asyncio
import contextlib
import time
from lwp_client import private_write
from device_protocol import decode_push,record_command,RECORD_URI,TRANSCRIPT_STREAMS
from live_diagnostics import PushCapture
from live_transcript import TranscriptStore
import rtc_protocol as protocol
import record_tasks as task_protocol
from mcs_signaling import MCS


async def run_probe(channel,config,directory,seconds,join_signaling=False,receive_audio=False,start_recording=False,inspect_record_task=False):
    if inspect_record_task and not (join_signaling and start_recording):
        raise ValueError('Task inspection requires an owned recording and conference join')
    if receive_audio:
        if not join_signaling:raise ValueError('Audio reception requires conference signalling')
        from mcs_media import AudioReceiver
    capture=PushCapture(directory);capture.start(120,connected=True,queue_dropped=channel.dropped_pushes)
    transcript=TranscriptStore();receipt=None;create_attempted=False;record_attempted=False;mcs=None;audio=None
    result={'seconds_requested':seconds,'recording_start_requested':start_recording,'recording_start_acknowledged':False,'mcs_signaling_login_attempted':join_signaling,'conference_join_acknowledged':False,'media_join_attempted':receive_audio,'translation_targets':0,'tts_requested':False,
            'physical_effects_verified':False,'requests':{},'error_type':None,'device_stop_acknowledged':False,
            'conference_close_acknowledged':False,'conference_end_readback':False}
    observed_task=None;task_read_job=None
    task_read=result['record_task_read']={'requested':inspect_record_task,'attempted':False,'matched':False,
        'real_time_asr':None,'physical_state_verified':False,'error_type':None,
        'reason':'no-matched-active-task' if inspect_record_task else 'not-requested'}
    pending_path=directory.parent/'rtc-probe-pending.json'
    journal={'probe_directory':directory.name,'pending':True,'created_at':time.time(),'target_device_id':config['target_device_id']}
    private_write(pending_path,journal)
    async def rpc(name,uri,body):
        nonlocal create_attempted,record_attempted,receipt
        cleanup=name in {'device-stop','conference-close','conference-info-after-close'}
        try:private_write(directory/(name+'-request.json'),{'uri':uri,'body':body,'sent_at':time.time()})
        except OSError:
            if not cleanup:raise
            result['journal_write_failed']=True
        if name=='create':create_attempted=True
        if name=='device-start':record_attempted=True
        reply=await channel._request(uri,body,{'referer':'https://n.qwennote.cn/qwennote/minutes/More.html'})
        if name=='create' and reply.get('code')==200:
            receipt=protocol.parse_receipt(config,reply)  # Retain cleanup identity even if receipt persistence fails.
        result['requests'][name]=protocol.reply_status(reply)
        try:private_write(directory/(name+'-response.json'),reply)
        except OSError:
            result['journal_write_failed']=True
            if not cleanup:raise
        return reply
    async def read_task(task,timeout):
        task_read.update(attempted=True,reason=None)
        try:
            async with asyncio.timeout(timeout):
                reply=await rpc('record-task-during-rtc',task_protocol.QUERY_URI,task_protocol.query(config,task.fid))
                parsed=task_protocol.parse_reply(config,reply,task.fid)
                task_read.update(matched=True,**parsed.summary())
        except asyncio.CancelledError:
            task_read['error_type']='CancelledError';raise
        except Exception as error:task_read['error_type']=type(error).__name__

    async def consume():
        nonlocal observed_task
        while True:
            capture.observe_queue(channel.dropped_pushes)
            try:message=await asyncio.wait_for(channel.pushes.get(),0.5)
            except asyncio.TimeoutError:continue
            error=False
            try:events=decode_push(message)
            except (ValueError,TypeError,KeyError,AttributeError):events=[];error=True
            if inspect_record_task:
                for event in events:
                    try:task=task_protocol.from_event(config,event)
                    except (ValueError,TypeError,KeyError):continue
                    if task is not None and task.state==0 and task.start_ms is not None and (journal['created_at']-5)*1000<=task.start_ms<=(time.time()+5)*1000:
                        if observed_task is None or task.order()>=observed_task.order():observed_task=task
            updates=sum(transcript.accept(e['payload'],source=TRANSCRIPT_STREAMS[e['type']]) is not None
                        for e in events if e['type'] in TRANSCRIPT_STREAMS)
            capture.observe_push(message,events,updates,decoder_error=error)
    receiver=asyncio.create_task(consume())
    try:
        if start_recording:
            start=await rpc('device-start',RECORD_URI,record_command(config,'start'))
            if start.get('code')!=200:raise ValueError('Device recording start rejected')
            result['recording_start_acknowledged']=True
        response=await rpc('create',protocol.CREATE_URI,protocol.create_transcription(config))
        receipt=protocol.parse_receipt(config,response)
        result['receipt']=receipt.summary()
        private_write(directory/'receipt.json',response)
        conf_period=0;conf_sequence=0;conf_updated=0;conf_beat=0
        if join_signaling:
            mcs=MCS(directory)
            login=await mcs.open(receipt.token,config.get('user_agent'))
            private_write(directory/'mcs-login-ack.json',login)
            joined=await rpc('conference-join',protocol.JOIN_URI,protocol.join_conference(receipt,mcs.participant))
            result['conference_join_acknowledged']=joined.get('code')==200 and isinstance(joined.get('body'),dict) and joined['body'].get('code')==200
            if not result['conference_join_acknowledged']:raise ValueError('Application conference join rejected')
            join_body=joined['body'];period=join_body.get('heartBeatTimeMillis')
            if type(period) is not int or not 1000<=period<=120000:raise ValueError('Invalid conference heartbeat period')
            conf_period=period/1000;conf_sequence=join_body.get('statusSeqNum',0)
            conf_updated=join_body.get('confInfoModel',{}).get('lastModifyTime',0)
        if receive_audio:
            audio=AudioReceiver(mcs);await audio.open(login)
        next_conf_heartbeat=time.monotonic()
        deadline=time.monotonic()+seconds
        next_keepalive=time.monotonic()+min(receipt.keepalive_seconds,5) if receipt.keepalive_seconds else float('inf')
        beat=0
        while time.monotonic()<deadline:
            if not channel.ready or (mcs is not None and not mcs.ready):raise ConnectionError('Probe transport disconnected')
            if audio is not None and audio.error_type is not None:raise ValueError('Media receiver failed')
            if conf_period and time.monotonic()>=next_conf_heartbeat:
                conf_beat+=1
                beat_reply=await rpc('conference-heartbeat-'+str(conf_beat),protocol.HEARTBEAT_URI,
                                     protocol.conference_heartbeat(receipt,mcs.participant,conf_sequence,conf_updated))
                if beat_reply.get('code')!=200 or beat_reply.get('body',{}).get('code')!=200:
                    raise ValueError('Conference heartbeat rejected')
                next_conf_heartbeat=time.monotonic()+conf_period
            if time.monotonic()>=next_keepalive:
                beat+=1
                reply=await rpc('keepalive-'+str(beat),protocol.UPDATE_URI,protocol.keepalive(config,receipt))
                if reply.get('code')!=200:raise ValueError('RTC keepalive rejected')
                next_keepalive=time.monotonic()+receipt.keepalive_seconds
            # One opt-in read alongside heartbeats, bounded by the observation window.
            if inspect_record_task and task_read_job is None and observed_task is not None:
                task_read_job=asyncio.create_task(read_task(observed_task,min(2,max(0,deadline-time.monotonic()))))
            await asyncio.sleep(min(0.2,max(0,deadline-time.monotonic())))
    except asyncio.CancelledError:
        result['error_type']='CancelledError'
        raise
    except Exception as error:
        result['error_type']=type(error).__name__
    finally:
        async def cleanup():
            if task_read_job is not None:
                if not task_read_job.done():task_read_job.cancel()
                with contextlib.suppress(asyncio.CancelledError):await task_read_job
            # An attempt alone does not establish ownership. A rejected/lost start may
            # mean another recording exists; never send Stop merely because we tried.
            owns_recording=result['recording_start_acknowledged'] or receipt is not None
            if (create_attempted or record_attempted) and not owns_recording:
                result['stop_withheld_reason']='recording-ownership-unconfirmed'
            if owns_recording:
                try:
                    reply=await rpc('device-stop',RECORD_URI,record_command(config,'stop'))
                    result['device_stop_acknowledged']=reply.get('code')==200
                except Exception as error:result['device_stop_error_type']=type(error).__name__
            if audio is not None:
                try:await audio.close()
                except Exception as error:result['audio_close_error_type']=type(error).__name__
                result['audio']=audio.summary()
            if receipt is not None:
                try:
                    reply=await rpc('conference-close',protocol.CLOSE_URI,protocol.close_conference(receipt))
                    result['conference_close_acknowledged']=reply.get('code')==200 and reply.get('body',{}).get('code')==200
                except Exception as error:result['conference_close_error_type']=type(error).__name__
                try:
                    info=await rpc('conference-info-after-close',protocol.INFO_URI,protocol.conference_info(receipt))
                    result['conference_end_readback']=protocol.closed_info(receipt,info)
                except Exception as error:result['conference_readback_error_type']=type(error).__name__
            if mcs is not None:
                try:await mcs.close()
                except Exception as error:result['mcs_close_error_type']=type(error).__name__
                result['mcs']=mcs.summary()
            await asyncio.sleep(1)
            receiver.cancel()
            try:await receiver
            except asyncio.CancelledError:pass
            except Exception as error:result['push_receiver_error_type']=type(error).__name__
            capture.observe_queue(channel.dropped_pushes);result['capture']=capture.stop()
            result['transcript_rows']=len(transcript.snapshot()['rows'])
            # A rejection is not an operational session. Unknown create/close outcomes retain ownership.
            rejected=result['requests'].get('create',{}).get('transport_code') in {400,401,403,404}
            settled=result['device_stop_acknowledged'] and (result['conference_end_readback'] or rejected or (record_attempted and not create_attempted))
            result['cleanup_pending']=not settled
            journal.update(pending=not settled,completed_at=time.time(),result=result)
            private_write(pending_path,journal);private_write(directory/'result.json',result)
        # Cancellation may arrive while a normal session is already closing.
        # Keep the first close/stop sequence alive; do not repeat an ambiguous write.
        closer=asyncio.create_task(cleanup())
        cancelled=False
        while not closer.done():
            try:await asyncio.shield(closer)
            except asyncio.CancelledError:
                cancelled=True;result['error_type']='CancelledError'
        await closer
        if cancelled:raise asyncio.CancelledError()
    return result

