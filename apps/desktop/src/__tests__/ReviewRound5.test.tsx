// Review of 78fb72a, the app half (probes ported first): an action shows only
// when the project defines its state and every key it names is there, and a
// withheld row id is never looked up. The Python half is test_work_review6.py.
import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { WorkView } from '../WorkView';
import { LedgerPage } from '../LedgerPage';
import type { Vocab, WorkView as Doc } from '../work';
const invoke = vi.fn();
vi.mock('@tauri-apps/api/core',()=>({invoke:(...args:unknown[])=>invoke(...args)}));
afterEach(()=>{cleanup();invoke.mockReset()});
const vocab:Vocab={state:[['NEEDS OWNER','needs'],['QUEUED','open']],kind:['slice']};
function doc(item:any):Doc {return {format:'khipu-work-view',version:1,generated_at:'2026-10-15T12:00:00Z',projects:[{project:'atlas',name:'Atlas',source:{state:'none'},jobs:[],prs:[],trains:[],merged:[],flagged:0,ledger:{rows:1,by_state:{'NEEDS OWNER':1},goals:[],loose_ends:[],loose_end_count:0,changed_today:[],vocab}}],live:[],waiting:[item],counts:{waiting:1,blocking:0,running:0,flagged:0,stale_sources:0,ledger_outbox:0}} as Doc}
function show(data:Doc){render(<WorkView work={{data,error:null,loading:false,readAt:null,load:async()=>{}}} routing={{data:null,error:null,loading:false,lastOkAt:null,firstRead:false,load:async()=>{}}} owedSection={null} owedFocusKey={0} onOwedClose={async()=>{}} onSnooze={()=>{}} onOpenRouting={()=>{}}/>)}
it('does not offer Done or Drop when neither state exists in the project vocabulary',()=>{
 show(doc({source:'ledger',key:'ledger:atlas#n1',project:'atlas',id:'n1',title:'Owner choice',blocking:false,actions:['done','drop','answered','open_ledger']}));
 expect(screen.queryByRole('button',{name:'Done'})).toBeNull();
 expect(screen.queryByRole('button',{name:'Drop'})).toBeNull();
});
it('does not offer These are the same when its project key was withheld',()=>{
 show(doc({source:'commitment',key:'commitment:42',project:null,id:'42',title:'Owner choice',blocking:false,looks_like:{id:'n1',title:'Similar choice'},actions:['done','snooze','hide','same','not_same']}));
 expect(screen.queryByRole('button',{name:'These are the same'})).toBeNull();
});
it('a row whose usable key was scrubbed offers no lookup action',async()=>{
 const r={id:'(a path)',project:'atlas',title:'Imported nested id',kind:'slice',parent:'',state:'QUEUED',evidence:'',owner:'none',updated:'2026-10-15T12:00:00Z',link:'',refs:{},history:[]};
 invoke.mockImplementation(async(cmd:string)=>JSON.stringify(cmd==='khipu_ledger_list'?{ok:true,project:'atlas',offline:false,vocab,rows:[r]}:cmd==='khipu_ledger_removed'?{removed:[]}:cmd==='khipu_ledger_get'?r:{ok:true}));
 render(<LedgerPage project='atlas' name='Atlas' view={null} onBack={()=>{}} onChanged={async()=>{}} onOwedClose={async()=>{}}/>);
 await screen.findByText('Imported nested id');
 fireEvent.click(screen.getByRole('button',{name:/Imported nested id/}));
 await act(async()=>{});
 expect(invoke.mock.calls.filter(([c])=>c==='khipu_ledger_get')).toEqual([]);
});
