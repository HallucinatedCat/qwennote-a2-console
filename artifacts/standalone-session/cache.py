"""Server-side cache for cloud read results, kept in protected storage so it survives restarts.

Design: note data is served from this cache for 24 hours and option
lists (languages, templates, organizations) for 7 days, to cut cloud traffic and load time.
Every entry carries tags (such as "note:<uuid>" or "notes-list"); writes drop the tags they
affect, and any request can bypass the cache with {"refresh": true}.
"""
import hashlib
import json
from pathlib import Path
import stat
import time
from lwp_client import private_write

DAY=24*3600
WEEK=7*DAY


class Cache:
    def __init__(self,directory,max_entries=5000):
        self.directory=Path(directory);self.max_entries=max_entries;self.entries={}
        self.directory.mkdir(mode=0o700,exist_ok=True)
        info=self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077:raise ValueError('Protected cache directory required')
        for path in self.directory.glob('*.json'):
            try:
                if path.is_symlink():raise ValueError('Cache entry must not be a link')
                entry=json.loads(path.read_text())
                self.entries[entry['key']]={'at':float(entry['at']),'tags':list(entry['tags']),'value':entry['value'],'path':path}
            except (OSError,ValueError,KeyError,TypeError):path.unlink(missing_ok=True)

    def _path(self,key):return self.directory/(hashlib.sha256(key.encode()).hexdigest()[:40]+'.json')

    def get(self,key,ttl):
        """The entry ({'at', 'value'}) if it is younger than ttl seconds, else None."""
        entry=self.entries.get(key)
        return entry if entry is not None and time.time()-entry['at']<=ttl else None

    def put(self,key,value,tags=()):
        path=self._path(key);at=time.time();tags=sorted(set(tags))
        private_write(path,{'key':key,'at':at,'tags':tags,'value':value})
        self.entries[key]={'at':at,'tags':tags,'value':value,'path':path}
        if len(self.entries)>self.max_entries:
            for old in sorted(self.entries,key=lambda k:self.entries[k]['at'])[:len(self.entries)-self.max_entries]:self._drop_key(old)

    def _drop_key(self,key):
        entry=self.entries.pop(key,None)
        if entry is not None:entry['path'].unlink(missing_ok=True)

    def drop(self,*tags):
        wanted=set(tags)
        for key in [key for key,entry in self.entries.items() if wanted&set(entry['tags'])]:self._drop_key(key)

    def clear(self):
        for key in list(self.entries):self._drop_key(key)

    def stats(self):
        return {'entries':len(self.entries),'oldest_at':min((e['at'] for e in self.entries.values()),default=None)}
