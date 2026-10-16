// Review of ec862b4, the app half (probes ported first): Answered never
// offers or sends a state the project doesn't define, and a removed row whose
// id the projection changed offers no Restore. The Python half is
// test_work_review7.py.
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { LedgerPage } from '../LedgerPage';
import { WorkView } from '../WorkView';
import type { Vocab, WorkView as Doc } from '../work';
const invoke = vi.fn();
vi.mock('@tauri-apps/api/core',()=>({invoke:(...args:unknown[])=>invoke(...args)}));
afterEach(()=>{cleanup();invoke.mockReset()});
const vocab:Vocab={state:[['WAIT','needs'],['RUN','open']],kind:['slice']};
function mountWork(v:Vocab=vocab){
 const data={format:'khipu-work-view',version:1,generated_at:'2026-10-16T12:00:00Z',
 projects:[{project:'atlas',name:'Atlas',source:{state:'none'},jobs:[],prs:[],trains:[],merged:[],flagged:0,
 ledger:{rows:1,by_state:{WAIT:1},goals:[],loose_ends:[],loose_end_count:0,changed_today:[],vocab:v}}],
 live:[],waiting:[{source:'ledger',key:'ledger:atlas#need',project:'atlas',id:'need',title:'Owner decision',
 blocking:false,actions:['done','drop','answered','open_ledger']}],
 counts:{waiting:1,blocking:0,running:0,flagged:0,stale_sources:0,ledger_outbox:0}} as unknown as Doc;
 render(<WorkView work={{data,error:null,loading:false,readAt:null,load:async()=>{}}}
 routing={{data:null,error:null,loading:false,lastOkAt:null,firstRead:false,load:async()=>{}}}
 owedSection={null} owedFocusKey={0} onOwedClose={async()=>{}} onSnooze={()=>{}} onOpenRouting={()=>{}}/>);
}
it('Answered defaults to the first state the project defines and Save sends what is shown',async()=>{
 invoke.mockResolvedValue(JSON.stringify({ok:true}));
 mountWork();
 fireEvent.click(screen.getByRole('button',{name:'Answered…'}));
 const select=screen.getByRole('combobox',{name:'New state'}) as HTMLSelectElement;
 expect(select.value).toBe('RUN');
 expect(Array.from(select.options).map((o)=>o.value)).toEqual(['RUN']);
 fireEvent.click(screen.getByRole('button',{name:'Save'}));
 await waitFor(()=>expect(invoke.mock.calls.some(([c])=>c==='khipu_ledger_set')).toBe(true));
 const args=invoke.mock.calls.find(([c])=>c==='khipu_ledger_set')![1];
 expect(args.change.state).toBe('RUN');
});
it('Answered still defaults to the done word when the project defines one',()=>{
 mountWork({state:[['WAIT','needs'],['RUN','open'],['SHIPPED','done']],kind:['slice']});
 fireEvent.click(screen.getByRole('button',{name:'Answered…'}));
 expect((screen.getByRole('combobox',{name:'New state'}) as HTMLSelectElement).value).toBe('SHIPPED');
});
it('a removed row with a projected invalid id offers no Restore',async()=>{
 invoke.mockImplementation(async(c:string)=>JSON.stringify(c==='khipu_ledger_list'?
 {ok:true,project:'atlas',offline:false,vocab,rows:[]}:
 c==='khipu_ledger_removed'?{removed:[{id:'(a path)',title:'Imported row',project:'atlas',kind:'slice',state:'RUN',
 removed_at:'2026-10-16T12:00:00Z',removed_by:'you, in the app',note:'',days_left:29}]}:{ok:true}));
 render(<LedgerPage project='atlas' name='Atlas' view={null} onBack={()=>{}} onChanged={async()=>{}} onOwedClose={async()=>{}}/>);
 fireEvent.click(await screen.findByRole('button',{name:'Removed (1)'}));
 await screen.findByText('Imported row');
 expect(screen.queryByRole('button',{name:'Restore (a path)'})).toBeNull();
 expect(invoke.mock.calls.filter(([c])=>c==='khipu_ledger_restore')).toEqual([]);
});
