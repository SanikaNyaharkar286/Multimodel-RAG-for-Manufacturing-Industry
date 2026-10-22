import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import '/src/styles/global.css';
import '/src/styles/chat.css';
import '/src/styles/profile.css';
import Header from '/src/components/layout/Header.jsx';
import Welcome from '/src/components/layout/Welcome.jsx';
import ChatInput from '/src/components/chat/ChatInput.jsx';
import ChatMessage from '/src/components/chat/ChatMessage.jsx';
import {AuthProfileProvider} from '/src/auth/AuthProfileContext.js';
import AdminPortal from '/src/components/AdminPortal.jsx';
const params=new URLSearchParams(location.search), view=params.get('view')||'welcome';
const workflow={workflow_id:'ui-fixture',status:'awaiting_done',machine:'HAB-3232',issue:'The saw blade is shifting during operation.',total_steps:3,plan_version:1,completed_steps:[],
 current_step:{step_id:'step-one',number:1,title:'Inspect the blade guide assembly',expected_result:'Record whether the blade guide is secure and report any visible movement.'}};
const response={repair_workflow:workflow,safety_warning:'Follow the authorized shutdown, isolation and verification procedure before inspection. Keep clear of moving parts.',
 blocks:[{type:'text',content:'### Step 1\nInspect the blade guide assembly using the approved manual. Record your observations before continuing.'}],
 sources:[{document_name:'HAB-3232 Operation Manual',page_number:42}]};
const report={report_id:'ui-fixture',report_number:'MR-PREVIEW',status:'Reported unresolved',session_id:'preview-conversation',case_id:'preview-case',
 technician_name:'Alex Morgan',created_at:'2026-10-22T10:00:00Z',timezone:'Asia/Calcutta',revision:1,title:'Blade guide inspection',machine:'Band saw',model:'HAB-3232',
 issue:workflow.issue,cause:'Not confirmed',action_taken:'Inspected the blade guide assembly.',verification:'Further inspection required.',history:[],workflow};
const originalFetch=window.fetch;
window.fetch=(url,options)=>String(url).includes('/reports/ui-fixture') ? Promise.resolve(new Response(JSON.stringify(report),{headers:{'Content-Type':'application/json'}}))
  : String(url).includes('/admin/requests') ? Promise.resolve(new Response(JSON.stringify({requests:[{uid:'demo-one',fullName:'Alex Morgan',email:'alex@example.com',department:'Maintenance',site:'Plant A',employeeId:'T-104',requestReason:'Equipment troubleshooting and repair documentation.',status:'pending'},{uid:'demo-two',fullName:'Sam Taylor',email:'sam@example.com',department:'Operations',site:'Plant B',employeeId:'T-105',status:'approved'}]}),{headers:{'Content-Type':'application/json'}})) : originalFetch(url,options);
function Preview(){
 const [question,setQuestion]=useState(''),[action,setAction]=useState('');
 if(view==='admin') return <AdminPortal user={{getIdToken:async()=> 'fixture'}} onClose={()=>setAction('back-chat')}/>;
 const input=<ChatInput question={question} setQuestion={setQuestion} loading={false} compact={view!=='welcome'} onSend={e=>{e.preventDefault();setAction('send');}} onImageSelect={()=>setAction('image')} onImageRemove={()=>{}}/>;
 return <AuthProfileProvider value={{profile:{name:'Alex Morgan',role:'technician'},logout(){},openProfile(){},openAdmin(){}}}>
 <div className={`app ${view==='welcome'?'':'app--chat'}`}><div className="chat-layout"><Header showNewChat={view!=='welcome'} onNewChat={()=>setAction('new-chat')}/>
 {view==='welcome'?<main className="main-container"><Welcome onExampleSelect={setQuestion}>{input}</Welcome></main>:<>
 <div className="chat-thread">
 <ChatMessage message={{role:'user',text:workflow.issue}}/>
 <ChatMessage message={{role:'assistant',status:'done',response:view==='report'?{blocks:[],documentation:report}:response}} repairActive={view!=='report'} onRepairAction={value=>setAction(value.action)}/>
 </div><div className="input-dock"><div className="dock-inner">{input}</div></div></>}
 <output id="qa-action" style={{display:'none'}}>{action}</output></div></div></AuthProfileProvider>;
}
createRoot(document.getElementById('root')).render(<Preview/>);
setTimeout(async()=>{
 const result={view,width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight,overflow:document.documentElement.scrollWidth>innerWidth,verticalOverflow:document.documentElement.scrollHeight>innerHeight};
 if(params.has('check')){
  if(view==='welcome'){
   document.querySelector('.example-buttons button').click();await new Promise(r=>setTimeout(r,50));
   result.exampleSetsQuestion=Boolean(document.querySelector('#chat-question').value);
   result.composerFocused=document.activeElement.id==='chat-question';
  }else if(view==='chat'){
   document.querySelector('.repair-details').click();await new Promise(r=>setTimeout(r,50));
   result.issueForm=Boolean(document.querySelector('.repair-issue-form textarea'));
   result.imageAttachment=Boolean(document.querySelector('.repair-issue-form input[type="file"]'));
  }else{
   result.modalFocus=Boolean(document.activeElement.closest('[role="dialog"]'));
   document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));await new Promise(r=>setTimeout(r,50));
   result.escapeCloses=!document.querySelector('[role="dialog"]');
  }
 }
 const output=document.createElement('pre');output.id='qa-results';output.hidden=true;output.textContent=JSON.stringify(result);document.body.append(output);
},1200);
