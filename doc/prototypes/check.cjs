const fs=require('fs'),vm=require('vm'),assert=require('assert');
const file=require('node:path').join(__dirname,'havenbox-v05.fragment.html');
const source=fs.readFileSync(file,'utf8');
function setup(){
 const root={innerHTML:'',listeners:{},style:{setProperty(){}},addEventListener(k,fn){this.listeners[k]=fn},querySelectorAll(){return [];},querySelector(selector){const node={focus(){},setSelectionRange(){},innerHTML:''};if(selector==='[data-action="send"]'){Object.defineProperty(node,'disabled',{set(value){root.innerHTML=root.innerHTML.replace(/(<button[^>]*data-action="send")([^>]*)(>)/,(all,first,attrs,last)=>first+attrs.replace(/ disabled/g,'')+(value?' disabled':'')+last);}});}return node;}};
 const context={document:{getElementById:id=>{assert.equal(id,'havenbox-v05');return root}},URL};
 const js=source.match(/<script>([\s\S]*?)<\/script>/)[1].replace('if(globalThis.Tweak){','globalThis.debug=()=>({sessions,artifacts,profiles,model,local,tools,combos,current:current(),active,panel,page,profileReady,canExecute,toolRevision});\nif(globalThis.Tweak){');
 vm.runInNewContext(js,context);
 function click(action){const marker='data-action="'+action+'"';const i=root.innerHTML.indexOf(marker);assert(i>=0,'missing button '+action);const disabled=root.innerHTML.slice(i,root.innerHTML.indexOf('>',i)).includes('disabled');if(!disabled)root.listeners.click({target:{closest:()=>({dataset:{action},disabled})}});return !disabled;}
 function change(field,value){const check=typeof value==='boolean';const select=['harness','binding','targetProfile','template','protocol'].includes(field);const target={dataset:{field},type:check?'checkbox':'text',tagName:select?'SELECT':'INPUT',value,checked:value};root.listeners[check||select?'change':'input']({target});}
 const state=()=>context.debug();
 const lastTask=()=>state().current.tasks.at(-1);
 function localTask(example='aggregate'){click('example:'+example);click('send');assert.equal(state().active.phase,'planning');click('reply');const t=lastTask();assert(t);click('execute:'+t.id);click('finish');return t;}
 function onlineProfile(h='Kimi CLI'){
 click('page:models');click('key');click('save-model');click('test-model');assert.equal(state().panel.type,'test');click('test-pass');
 click('page:tools');change('harness',h);change('binding','api');click('verify-combo');click('save-profile');return state().profiles.at(-1);
 }
 return {root,click,change,state,lastTask,localTask,onlineProfile};
}
{
 const t=setup();let first=t.localTask();assert.equal(first.status,'done');assert.equal(first.output,t.state().artifacts[t.state().current.artifacts[0]].name,'approved output filename must match artifact');assert.equal(t.state().artifacts[t.state().current.artifacts[0]].total,700);
 t.click('example:answer');t.click('send');assert.equal(t.state().active.phase,'answering');t.click('reply');assert.equal(t.state().current.tasks.length,1,'read-only explanation must not add execution task');
 t.localTask('reaggregate');const a=t.state().artifacts[t.state().current.artifacts.at(-1)];assert.equal(a.total,720);assert.equal(a.count,5);assert.equal(t.state().current.artifacts.length,2);
 t.click('preview:'+a.id);t.click('export:'+a.id);assert(a.exported);assert(t.root.innerHTML.includes('未写入宿主文件'));
 t.click('manage-session');t.change('rename','汇总 <img src=x>');t.click('rename');assert(t.root.innerHTML.includes('&lt;img src=x&gt;'));assert(!t.root.innerHTML.includes('<img src=x>'));
 t.click('switch');t.change('carryFiles',true);t.change('carryText',true);t.click('create');assert.equal(t.state().current.artifacts.length,2);assert(t.state().current.messages[0].text.startsWith('带入摘要'));
 t.click('session:c1');t.click('manage-session');t.click('delete-dialog');t.click('delete');assert(t.state().artifacts[a.id],'shared artifact must survive deletion of source session');
}
{
 const t=setup();t.click('send');const owner=t.state().current.id;t.click('new');t.click('create');const other=t.state().current.id;assert.notEqual(other,owner);t.change('draft','另一条会话的草稿');assert(!t.click('send'),'no concurrent send');assert(t.root.innerHTML.includes('返回活动会话'));
 t.click('return-active');t.click('stop');assert.equal(t.state().active.phase,'stopping');t.click('new');t.click('create');assert(!t.click('send'));t.click('return-active');t.click('clean');assert.equal(t.state().active,null);assert(t.state().current.messages.at(-1).interrupted);
}
{
 const t=setup(),p=t.onlineProfile();assert(t.state().profileReady(p));const localP=t.state().profiles[0];assert(t.state().profileReady(localP),'API changes cannot invalidate local profile');
 t.click('page:models');t.click('model-local');t.click('local-missing');assert(t.state().profileReady(p),'missing local model cannot invalidate online profile');assert(!t.state().profileReady(localP));
 t.click('page:chat');t.click('switch');t.change('targetProfile',p.id);t.click('create');t.click('import');t.change('draft','按门店汇总销售额');t.click('send');assert.equal(t.state().active,null,'must authorize before planning');assert.equal(t.state().panel.type,'consent');t.click('cancel-panel');assert.equal(t.state().current.messages.length,0);
 t.click('send');t.click('authorize');assert.equal(t.state().active.phase,'planning');t.click('reply');const task=t.lastTask();t.click('execute:'+task.id);t.click('finish');assert.equal(t.state().artifacts[t.state().current.artifacts.at(-1)].total,700);
 t.click('example:answer');t.click('send');t.change('consentResults',false);assert(!t.click('authorize'),'result data required for explanation');t.change('consentResults',true);t.click('authorize');t.click('reply');assert.equal(t.state().current.tasks.length,1);
 t.click('page:tools');t.change('harness','Pi');t.change('binding','api');t.click('verify-combo');t.click('save-profile');assert.equal(t.state().profiles.filter(p=>p.m==='api').length,2,'one model reusable by two harnesses');
 t.click('page:models');t.click('model-api');t.change('name','修改后的连接名称');assert.equal(p.provider,'示例 API 连接','historical route snapshot must not mutate');assert(!t.state().profileReady(p));
}
{
 const t=setup();t.click('send');t.click('reply');const task=t.lastTask();t.click('manage-session');t.click('archive');assert(!t.click('execute:'+task.id),'archived pending plan cannot execute');t.click('manage-session');t.click('archive');assert(!t.click('execute:'+task.id),'unarchived history requires recovery check');t.click('recover-check');assert(t.click('execute:'+task.id));t.click('stop');t.click('cleanup-fail');assert.equal(t.state().active.phase,'cleanup');t.click('new');t.click('create');assert(!t.click('send'));
}
{
 const t=setup();t.click('session:c2');assert(t.state().current.unknown);assert(!t.click('recover-check'));t.click('inspect-unknown');assert(!t.state().current.unknown);t.click('recover-check');assert(!t.state().current.readOnly);assert(t.state().current.messages.at(-1).interrupted,'history must remain marked incomplete');
 t.click('page:tools');t.click('tools-tab:cli');t.click('discover:cli');assert.equal(t.state().tools.cli.allowed.length,0);assert(!t.click('enable:cli'));t.change('tool:cli:aggregate',true);t.click('enable:cli');t.click('cli-plan');const task=t.lastTask();assert(task.route.includes('不调用模型'));assert.equal(t.state().active,null);t.click('execute:'+task.id);t.click('fail');assert.equal(task.status,'failed');t.click('page:records');assert(t.root.innerHTML.includes('exit: 2'));
}
{
 const t=setup();t.change('draft','安装依赖并运行任意代码');t.click('send');t.click('reply');assert.equal(t.state().current.tasks.length,0);assert(t.root.innerHTML.includes('未生成执行计划'));
 t.change('draft','保留在当前会话的草稿');t.click('session:c2');assert.equal(t.state().current.draft,'');t.click('session:c1');assert.equal(t.state().current.draft,'保留在当前会话的草稿');
 t.click('page:models');t.click('model-local');t.click('local-missing');t.click('page:tools');t.click('tools-tab:cli');t.click('discover:cli');t.change('tool:cli:aggregate',true);t.click('enable:cli');t.click('cli-plan');const task=t.lastTask();assert(t.click('execute:'+task.id),'deterministic CLI must work with no local or API model');t.click('finish');assert.equal(task.status,'done');
 t.click('page:settings');t.click('restart');assert(t.state().current.recovery);assert(t.state().current.messages.length>0);t.click('recover-check');assert(t.state().current.readOnly,'invalid model must not fake native recovery');t.click('page:settings');t.click('reset-dialog');assert(t.state().current.artifacts.length>0);t.click('reset');assert.equal(t.state().sessions.length,1);assert.equal(Object.keys(t.state().artifacts).length,0);assert(t.state().tools.cli.enabled,'workspace reset does not delete tool configuration');
}
{
 const t=setup();t.click('send');t.click('reply');t.click('execute:'+t.lastTask().id);t.click('validation-fail');assert.equal(t.lastTask().status,'failed');assert(t.lastTask().validationFailure);t.click('page:records');assert(t.root.innerHTML.includes('output validation: failed'));
 t.click('page:chat');t.click('example:aggregate');t.click('send');t.click('stop');t.click('cleanup-fail');assert.equal(t.state().active.phase,'cleanup');t.click('close-workspace');assert.equal(t.state().active,null);assert(t.state().current.recovery);assert(t.state().current.readOnly);
}
assert(fs.readFileSync(require('node:path').join(__dirname,'index.html'),'utf8').includes(source),'generated page is stale; run build.cjs');
assert(!/fetch\(|XMLHttpRequest|WebSocket|localStorage|sessionStorage/.test(source));
console.log('PASS: 多轮计算 700/720、问答免执行确认、成果与引用隔离、会话管理、单活动/停止清理、联网先授权、权限范围、模型复用与快照、两种恢复、MCP/CLI 参数与失败等状态逻辑。未执行真实浏览器、网络、文件或进程。');
