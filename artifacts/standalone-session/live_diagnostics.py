"""Bounded, opt-in private push evidence. Never sends a cloud/device command."""
import json
import os
from pathlib import Path
import secrets
import stat
import time
from lwp_client import private_write
from device_protocol import TRANSCRIPT_STREAMS
from public_errors import Refused

COUNTERS=('push_frames','device_events','transcript_candidates','transcript_updates',
          'unrecognized_frames','decoder_errors','queue_dropped','connections','disconnects',
          'dinger_transcript_candidates','conference_transcript_candidates')
REASONS={'manual','time_limit','byte_limit','row_limit','io_error','shutdown'}


def protected_directory(path):
    """Refuse links and publicly accessible capture stores; preserve existing evidence."""
    path=Path(path)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077:
        raise ValueError('Protected capture directory required')


class PushCapture:
    def __init__(self,state,*,clock=time.monotonic,wall_clock=time.time,
                 byte_limit=8*1024*1024,row_limit=5000):
        self.root=Path(state)/'live-captures';self.clock=clock;self.wall_clock=wall_clock
        self.byte_limit=byte_limit;self.row_limit=row_limit;self.fd=None;self.directory=None
        self.started=0;self.deadline=0;self.last_dropped=0
        self.data={'capture_id':None,'active':False,'finish_reason':None,'elapsed_seconds':0,
                   'bytes_written':0,'rows_written':0,'summary_saved':False,
                   'hardware_behavior_verified':False,**dict.fromkeys(COUNTERS,0)}

    def start(self,seconds=120,*,connected=False,queue_dropped=0):
        self.tick()
        if self.fd is not None:raise Refused('A capture is already active')
        if isinstance(seconds,bool) or not isinstance(seconds,int) or not 1<=seconds<=300:
            raise Refused('Capture duration must be 1 to 300 seconds')
        protected_directory(self.root.parent)
        try:self.root.mkdir(mode=0o700)
        except FileExistsError:pass
        protected_directory(self.root)
        entries=list(self.root.iterdir())
        if len(entries)>=16:raise Refused('Capture storage limit reached; preserve or remove old captures locally')
        total=0
        for directory in entries:
            protected_directory(directory)
            for file in directory.iterdir():
                info=file.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise Refused('Unexpected capture storage entry')
                total+=info.st_size
        if total+self.byte_limit>64*1024*1024:raise Refused('Capture storage quota reached')
        identifier=secrets.token_hex(12);directory=self.root/identifier
        directory.mkdir(mode=0o700)
        data={'capture_id':identifier,'active':True,'finish_reason':None,
              'started_at':self.wall_clock(),'duration_limit_seconds':seconds,'elapsed_seconds':0,
              'bytes_written':0,'rows_written':0,'summary_saved':False,
              'hardware_behavior_verified':False,**dict.fromkeys(COUNTERS,0)}
        # An interrupted process leaves active=true in this private file. No capture resumes automatically.
        private_write(directory/'summary.json',data)
        self.fd=os.open(directory/'pushes.jsonl',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        self.directory=directory;self.data=data;self.started=self.clock();self.deadline=self.started+seconds
        self.last_dropped=max(0,queue_dropped)
        if connected:self.data['connections']=1
        return self.snapshot()

    def tick(self):
        if self.fd is not None:
            self.data['elapsed_seconds']=round(max(0,self.clock()-self.started),3)
            if self.clock()>=self.deadline:self.stop('time_limit')

    def snapshot(self):
        self.tick()
        return dict(self.data)

    def observe_queue(self,dropped):
        self.tick()
        if self.fd is None:return
        if isinstance(dropped,int) and not isinstance(dropped,bool) and dropped>=0:
            self.data['queue_dropped']+=max(0,dropped-self.last_dropped)
            self.last_dropped=dropped

    def connection(self,connected,queue_dropped=0):
        self.tick()
        if self.fd is None:return
        if connected:
            self.data['connections']+=1;self.last_dropped=0;self.observe_queue(queue_dropped)
        else:
            self.observe_queue(queue_dropped);self.data['disconnects']+=1
        self._append({'kind':'connection','connected':bool(connected)})

    def observe_push(self,message,events,updates,*,decoder_error=False):
        self.tick()
        if self.fd is None:return
        self.data['push_frames']+=1
        self.data['device_events']+=sum(e.get('type')==770000 for e in events)
        self.data['transcript_candidates']+=sum(e.get('type') in TRANSCRIPT_STREAMS for e in events)
        self.data['dinger_transcript_candidates']+=sum(e.get('type')==770001 for e in events)
        self.data['conference_transcript_candidates']+=sum(e.get('type')==86000 for e in events)
        self.data['transcript_updates']+=updates
        self.data['unrecognized_frames']+=int(not events and not decoder_error)
        self.data['decoder_errors']+=int(decoder_error)
        # Only incoming push packets reach here. No registration headers, requests or assistant traffic.
        self._append({'kind':'push','packet':message,'decoded_events':len(events),
                      'transcript_updates':updates,'decoder_error':bool(decoder_error)})

    def _append(self,row):
        if self.fd is None:return
        try:
            encoded=(json.dumps({'elapsed_seconds':round(self.clock()-self.started,3),**row},
                                ensure_ascii=False,separators=(',',':'))+'\n').encode()
            if self.data['bytes_written']+len(encoded)>self.byte_limit:
                self.stop('byte_limit');return
            view=memoryview(encoded)
            while view:
                count=os.write(self.fd,view)
                if count<=0:raise OSError('Capture write failed')
                view=view[count:];self.data['bytes_written']+=count
            self.data['rows_written']+=1
            if self.data['rows_written']>=self.row_limit:self.stop('row_limit')
            elif self.data['bytes_written']>=self.byte_limit:self.stop('byte_limit')
        except (OSError,ValueError,TypeError,RecursionError):
            self.stop('io_error')

    def stop(self,reason='manual'):
        if reason not in REASONS:raise ValueError('Unknown capture stop reason')
        if self.fd is None:return dict(self.data)
        fd=self.fd;self.fd=None;self.data['active']=False;self.data['finish_reason']=reason
        self.data['elapsed_seconds']=round(max(0,self.clock()-self.started),3)
        try:os.fsync(fd)
        except OSError:self.data['finish_reason']='io_error'
        finally:
            try:os.close(fd)
            except OSError:self.data['finish_reason']='io_error'
        try:
            self.data['summary_saved']=True
            private_write(self.directory/'summary.json',self.data)
        except (OSError,ValueError,TypeError):
            self.data['summary_saved']=False;self.data['finish_reason']='io_error'
        return dict(self.data)
