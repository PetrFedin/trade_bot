from __future__ import annotations
import argparse,hashlib,json,tempfile,threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
from typing import Sequence
from app.runtime.worker_execution_plane_v104 import *

class Transport:
    def get(self,url,*,headers,timeout_seconds,tls_verify,allow_redirects): return {"url":url,"ok":True}
class Store:
    def __init__(self): self.parts={}; self.lock=threading.Lock()
    def create_upload(self,key,metadata): return key
    def upload_part(self,upload_id,part_number,data,digest):
        with self.lock:self.parts[(upload_id,part_number)]=bytes(data)
        return digest
    def complete_upload(self,upload_id,parts,total_digest):
        data=b"".join(self.parts[(upload_id,n)] for n,_ in parts); return hashlib.sha256(data).hexdigest()
    def abort_upload(self,upload_id): pass

def run_one(index:int,root:Path):
    now=datetime(2026,8,3,18,tzinfo=timezone.utc); policy=WorkerPolicyV104(f"p-{index}",index+1,multipart_part_bytes=1024)
    ring=HmacKeyRingV104({"k":b"x"*32}); worker=f"w-{index}"; deploy=f"d-{index}"
    att=ring.sign_attestation(WorkerAttestationV104(worker,deploy,"sha256:"+"a"*64,"b"*40,policy.digest,policy.generation,now,now+timedelta(minutes=5),f"a-{index}","k"))
    claim=ring.sign_claim(SignedWorkClaimV104(f"c-{index}",f"camp-{index}",f"run-{index}",policy.generation,index+1,("account","orders","positions","clock"),now,now,now+timedelta(minutes=2),worker,deploy,policy.digest,f"n-{index}","k"))
    base=root/str(index); spool=EvidenceSpoolV104(base/"spool",10,10_000_000)
    plane=WorkerExecutionPlaneV104(policy,ring,ReplayLedgerV104(),ReadOnlyAlpacaRunnerV104(Transport(),"id","secret"),spool,ResumableUploaderV104(base/"uploads",Store(),1024,b"z"*32),DeadLetterQueueV104(base/"dlq"),WorkerEventJournalV104(base/"events"))
    result=plane.execute(claim,att,now); return result.outcome.value,plane.journal.verify()[-1].event_digest

def main(argv:Sequence[str]|None=None):
    p=argparse.ArgumentParser();p.add_argument("--iterations",type=int,default=1000);p.add_argument("--workers",type=int,default=8);a=p.parse_args(argv)
    failures=[];digests=[]
    with tempfile.TemporaryDirectory() as temp,ThreadPoolExecutor(max_workers=a.workers) as pool:
        for i,future in enumerate(pool.map(lambda i:run_one(i,Path(temp)),range(a.iterations))):
            outcome,digest=future;digests.append(digest)
            if outcome!="VERIFIED":failures.append(i)
    report={"schema":104,"iterations":a.iterations,"workers":a.workers,"failures":len(failures),"unique_tail_digests":len(set(digests))}
    print(json.dumps(report,sort_keys=True));return 0 if not failures and len(set(digests))==a.iterations else 2
if __name__=="__main__":raise SystemExit(main())
