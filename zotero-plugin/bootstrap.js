var LiteratureAgent = {
  baseURL: "http://127.0.0.1:8765",
  expectedServerVersion: "0.6.0",
  pluginID: "literature-agent@research.local",
  contextMenuRegistrationID: null,
  menuID: "literature-agent-menu",
  pythonw: "C:\\Python313\\pythonw.exe",
  serverScript: "D:\\Literature-Agent\\agent_server.py",
  serverCWD: "D:\\Literature-Agent",
  starting: false,

  sleep(ms){ return new Promise(resolve => setTimeout(resolve, ms)); },

  async rawRequest(path, method="GET", body=null, timeout=2500) {
    let options={method,headers:{"Accept":"application/json"},timeout};
    if(body!==null){options.headers["Content-Type"]="application/json";options.body=JSON.stringify(body)}
    let xhr=await Zotero.HTTP.request(method,this.baseURL+path,options);
    let data; try{data=JSON.parse(xhr.responseText||"{}")}catch(e){data={raw:xhr.responseText}}
    if(xhr.status<200||xhr.status>=300)throw new Error(data.error||data.message||data.raw||`HTTP ${xhr.status}`);
    return data;
  },

  async spawnServer() {
    let CU = globalThis.ChromeUtils;
    let mod = CU.importESModule("resource://gre/modules/Subprocess.sys.mjs");
    let Subprocess = mod.Subprocess || mod.default || mod;
    if(!Subprocess || !Subprocess.call) throw new Error("Zotero subprocess API is unavailable.");
    // Spawn pythonw with no console window. Do not await process exit.
    await Subprocess.call({
      command: this.pythonw,
      arguments: [this.serverScript],
      workdir: this.serverCWD,
      stdout: "ignore",
      stderr: "ignore"
    });
  },

  async ensureServer(forceRestart=false) {
    if(this.starting) {
      for(let i=0;i<20;i++){ await this.sleep(250); try{return await this.rawRequest('/status')}catch(e){} }
      throw new Error('Agent server did not become ready.');
    }
    this.starting=true;
    try {
      let current=null;
      try { current=await this.rawRequest('/status'); } catch(e) {}
      if(current && !forceRestart && current.version===this.expectedServerVersion) return current;
      if(current) {
        try { await this.rawRequest('/shutdown','POST',{}); } catch(e) {}
        await this.sleep(700);
      }
      await this.spawnServer();
      for(let i=0;i<30;i++){
        await this.sleep(250);
        try {
          let s=await this.rawRequest('/status');
          if(s && s.ok) return s;
        } catch(e) {}
      }
      throw new Error('Could not start Literature Agent server. Check D:\\Literature-Agent\\agent.log.');
    } finally { this.starting=false; }
  },

  async request(path, method="GET", body=null) {
    await this.ensureServer(false);
    try { return await this.rawRequest(path,method,body,120000); }
    catch(first) {
      // One self-healing retry if the local server died between health check and request.
      await this.ensureServer(true);
      return await this.rawRequest(path,method,body,120000);
    }
  },

  alert(title,message){Services.prompt.alert(null,title,String(message))},
  selectedItemKey(){let pane=Zotero.getActiveZoteroPane();if(!pane)throw new Error("No active Zotero pane.");let items=pane.getSelectedItems();if(!items||items.length!==1)throw new Error("Select exactly one paper.");let item=items[0];if(item.isAttachment&&item.isAttachment()&&item.parentItemID){let p=Zotero.Items.get(item.parentItemID);if(p&&p.key)return p.key}if(!item.key)throw new Error("Selected item has no key.");return item.key},
  async status(){try{let s=await this.ensureServer(false);this.alert("Literature Agent",[`Service: Online`,`Version: ${s.version||"Unknown"}`,`Model: ${s.model||"Unknown"}`,`Task running: ${s.running?"Yes":"No"}`,`Mode: ${s.mode||"-"}`,`Last result: ${s.last_result||"-"}`,`Last error: ${s.last_error||"-"}`].join("\n"))}catch(e){this.alert("Literature Agent","Service unavailable.\n\n"+e.message)}},
  async scan(){try{let r=await this.request("/scan","POST",{});this.alert("Literature Agent",r.message)}catch(e){this.alert("Literature Agent",e.message)}},
  async processItemKey(key,force=false){try{let r=await this.request("/process","POST",{item_key:key,force});this.alert("Literature Agent",r.message)}catch(e){this.alert("Literature Agent",e.message)}},
  async processSelected(force=false){try{let key=this.selectedItemKey();return await this.processItemKey(key,force)}catch(e){this.alert("Literature Agent",e.message)}},
  async relatedItemKey(key){try{let r=await this.request('/related/'+key);let lines=(r.related||[]).map((x,i)=>`${i+1}. ${x.title} (${x.year||'-'})\n${x.relationship} · ${x.score}/100\n${x.reason}`);this.alert('Related Papers',lines.length?lines.join('\n\n'):'No related papers found.')}catch(e){this.alert('Literature Agent',e.message)}},
  async relatedSelected(){try{let key=this.selectedItemKey();return await this.relatedItemKey(key)}catch(e){this.alert('Literature Agent',e.message)}},
  async showResearchCardItemKey(key){try{let r=await this.request('/item/'+key+'/card');this.alert('Research Card',r.card||'No Research Card found. Analyze the paper first.')}catch(e){this.alert('Literature Agent',e.message)}},
  contextPaper(context){
    let items=context && context.items;
    if(!items || items.length!==1) return null;
    let item=items[0];
    if(!item) return null;
    if(item.isRegularItem && item.isRegularItem()) return item;
    if(item.parentItemID){
      let parent=Zotero.Items.get(item.parentItemID);
      if(parent && parent.isRegularItem && parent.isRegularItem()) return parent;
    }
    return null;
  },
  contextPaperKey(context){let item=this.contextPaper(context);return item&&item.key?item.key:null},

  async analyzeItemKey(key){
    try {
      let force=false;
      // One command covers first analysis and re-analysis.
      // If a Research Card already exists, ask before replacing the old AI note.
      try {
        let card=await this.request('/item/'+key+'/card');
        if(card && card.card){
          let ok=Services.prompt.confirm(
            null,
            'Literature Agent',
            'This paper already has an analysis. Replace it with a fresh analysis?'
          );
          if(!ok) return;
          force=true;
        }
      } catch(e) {
        // No existing card (or older record): process normally.
        force=false;
      }
      return await this.processItemKey(key,force);
    } catch(e) {
      this.alert('Literature Agent',e.message);
    }
  },

  ensureFTL(window){
    try {
      if(window && window.MozXULElement){
        window.MozXULElement.insertFTLIfNeeded('literature-agent.ftl');
      }
    } catch(e) {
      Zotero.debug('Literature Agent FTL load failed: '+e);
    }
  },

  registerContextMenu(){
    if(this.contextMenuRegistrationID || !Zotero.MenuManager || !Zotero.MenuManager.registerMenu) return;
    let self=this;
    this.contextMenuRegistrationID=Zotero.MenuManager.registerMenu({
      menuID:'literature-agent-item-context-analyze',
      pluginID:this.pluginID,
      target:'main/library/item',
      menus:[{
        menuType:'menuitem',
        l10nID:'literature-agent-context-analyze',
        onShowing:(_event,context)=>{
          context.setVisible(!!self.contextPaperKey(context));
        },
        onCommand:(_event,context)=>{
          let key=self.contextPaperKey(context);
          if(key) self.analyzeItemKey(key);
        }
      }]
    });
  },
  unregisterContextMenu(){
    if(!this.contextMenuRegistrationID) return;
    try{Zotero.MenuManager.unregisterMenu(this.contextMenuRegistrationID)}catch(e){}
    this.contextMenuRegistrationID=null;
  },
  async openDashboard(){try{await this.ensureServer(false);Zotero.launchURL(this.baseURL+"/dashboard")}catch(e){this.alert('Literature Agent',e.message)}},
  async restartServer(){try{let s=await this.ensureServer(true);this.alert('Literature Agent','Server restarted.\nVersion: '+(s.version||'-'))}catch(e){this.alert('Literature Agent',e.message)}},
  addMenu(window){if(!window||!window.document)return;let doc=window.document;if(doc.getElementById(this.menuID))return;let tools=doc.getElementById("menu_ToolsPopup");if(!tools)return;let menu=doc.createXULElement("menu");menu.id=this.menuID;menu.setAttribute("label","Literature Agent");let popup=doc.createXULElement("menupopup");let add=(label,fn)=>{let i=doc.createXULElement("menuitem");i.setAttribute("label",label);i.addEventListener("command",fn);popup.appendChild(i)};add("Open Control Center",()=>this.openDashboard());add("Agent Status",()=>this.status());add("Restart Agent Service",()=>this.restartServer());add("Scan Now",()=>this.scan());popup.appendChild(doc.createXULElement("menuseparator"));add("Process Selected Paper",()=>this.processSelected(false));add("Re-summarize Selected Paper",()=>this.processSelected(true));add("Find Related Papers",()=>this.relatedSelected());menu.appendChild(popup);tools.appendChild(menu)},
  removeMenu(window){if(!window||!window.document)return;let m=window.document.getElementById(this.menuID);if(m)m.remove()}
};
function install(data,reason){}
async function startup({id,version,rootURI},reason){await Zotero.initializationPromise;let ws=Zotero.getMainWindows?Zotero.getMainWindows():[Zotero.getMainWindow()];for(let w of ws){LiteratureAgent.ensureFTL(w);LiteratureAgent.addMenu(w)}LiteratureAgent.registerContextMenu();LiteratureAgent.ensureServer(false).catch(e=>Zotero.debug('Literature Agent autostart failed: '+e));}
function onMainWindowLoad({window},reason){LiteratureAgent.ensureFTL(window);LiteratureAgent.addMenu(window);LiteratureAgent.ensureServer(false).catch(e=>Zotero.debug('Literature Agent autostart failed: '+e));}
function onMainWindowUnload({window},reason){LiteratureAgent.removeMenu(window)}
function shutdown(data,reason){if(reason===APP_SHUTDOWN)return;LiteratureAgent.unregisterContextMenu();let ws=Zotero.getMainWindows?Zotero.getMainWindows():[Zotero.getMainWindow()];for(let w of ws)LiteratureAgent.removeMenu(w)}
function uninstall(data,reason){}
