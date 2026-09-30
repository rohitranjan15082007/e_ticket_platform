// Run through the connected Figma Plugin API; this is design source, not app code.
const WHICH = __WHICH__;
const createdNodeIds = [];
const C = {
  canvas: '#F5F5F1', ink: '#20223B', muted: '#667085', purple: '#5446A4',
  deep: '#292450', mint: '#D9F4C2', mintInk: '#286D53', lilac: '#ECE9F8',
  white: '#FFFFFF', line: '#E2E3EB', gold: '#FFE8B8', sand: '#FFF8E7',
  red: '#A94150', blush: '#FCEDEF', slate: '#41415E',
};
const color = hex => ({r:parseInt(hex.slice(1,3),16)/255,g:parseInt(hex.slice(3,5),16)/255,b:parseInt(hex.slice(5,7),16)/255});
const paint = hex => ({type:'SOLID',color:color(hex)});
await Promise.all([
  figma.loadFontAsync({family:'Inter',style:'Regular'}),
  figma.loadFontAsync({family:'Inter',style:'Medium'}),
  figma.loadFontAsync({family:'Inter',style:'Semi Bold'}),
  figma.loadFontAsync({family:'Inter',style:'Bold'}),
  figma.loadFontAsync({family:'Inter',style:'Extra Bold'}),
]);
const [buttonSet,inputSet] = await Promise.all([
  figma.importComponentSetByKeyAsync('cc8b558dc7d9684011b6b99ce8e6509399bc836b'),
  figma.importComponentSetByKeyAsync('c28150b04d333d34ed9d2b77abd9f2f54e1a878a'),
]);
const buttonPrimary = buttonSet.children.find(v=>v.name==='Variant=Primary, State=Default, Size=Medium');
const buttonNeutral = buttonSet.children.find(v=>v.name==='Variant=Neutral, State=Default, Size=Medium');
const inputDefault = inputSet.children.find(v=>v.name==='State=Default, Value Type=Placeholder');
if (!buttonPrimary || !buttonNeutral || !inputDefault) throw new Error('Required design-system variants unavailable');
function box(parent,name,w,h,fill=C.white,radius=0,direction='VERTICAL',pad=0,gap=0) {
  const f=figma.createAutoLayout(direction);
  f.name=name;f.resize(w,h);f.primaryAxisSizingMode='FIXED';f.counterAxisSizingMode='FIXED';
  f.fills=fill?[paint(fill)]:[];f.cornerRadius=radius;f.itemSpacing=gap;
  f.paddingLeft=pad;f.paddingRight=pad;f.paddingTop=pad;f.paddingBottom=pad;
  if(parent) parent.appendChild(f);createdNodeIds.push(f.id);return f;
}
function label(parent,value,size=16,weight='Regular',fill=C.ink,width=0) {
  const t=figma.createText();t.name=value.slice(0,42);t.fontName={family:'Inter',style:weight};
  t.fontSize=size;t.fills=[paint(fill)];t.textAutoResize=width?'HEIGHT':'WIDTH_AND_HEIGHT';
  if(width)t.resize(width,Math.max(22,size*1.4));t.characters=value;parent.appendChild(t);
  createdNodeIds.push(t.id);return t;
}
function space(parent,w,h) { const s=figma.createFrame();s.name='Space';s.resize(w,h);s.fills=[];parent.appendChild(s);createdNodeIds.push(s.id);return s; }
function tag(parent,value,bg=C.lilac,ink=C.purple) {
  const w=Math.max(72,value.length*7.5+26);const f=box(parent,'Tag / '+value,w,29,bg,15,'HORIZONTAL',7,0);
  f.primaryAxisAlignItems='CENTER';f.counterAxisAlignItems='CENTER';label(f,value,11,'Bold',ink);return f;
}
function button(parent,value,neutral=false) {
  const source=neutral?buttonNeutral:buttonPrimary;const inst=source.createInstance();inst.name='Button / '+value;
  inst.setProperties({'Label#2:0':value,'Has Icon Start#4:128':false,'Has Icon End#4:64':false});
  parent.appendChild(inst);createdNodeIds.push(inst.id);return inst;
}
function field(parent,name,value='Enter '+name.toLowerCase()) {
  const inst=inputDefault.createInstance();inst.name='Input / '+name;
  inst.setProperties({'Label#280:41':name,'Value#630:14':value,'Has Error#72:6':false});
  inst.resize(370,70);parent.appendChild(inst);createdNodeIds.push(inst.id);return inst;
}
function screen(id,title,index) {
  const f=box(null,`${String(index+1).padStart(2,'0')} / ${title}`,1440,900,C.canvas,0,'VERTICAL',0,0);
  figma.currentPage.appendChild(f);f.x=100+(index%3)*1540;f.y=100+Math.floor(index/3)*1000;
  return f;
}
function header(parent,active='Explore') {
  const h=box(parent,'Global header',1440,76,C.white,0,'HORIZONTAL',22,34);
  h.counterAxisAlignItems='CENTER';
  const brand=box(h,'Brand',210,44,C.deep,13,'HORIZONTAL',11,8);brand.counterAxisAlignItems='CENTER';
  label(brand,'✦',22,'Bold',C.mint);label(brand,'ETICKET',17,'Extra Bold',C.white);
  space(h,160,1);
  for(const name of ['Explore','Packages','Results','Wallet','My account']) {
    const ink=name===active?C.purple:C.muted;label(h,name,14,name===active?'Bold':'Medium',ink);
  }
  space(h,35,1);tag(h,'DESIGN / SAMPLE DATA',C.sand,C.red);
  return h;
}
function side(parent,active,admin=false) {
  const s=box(parent,admin?'Admin sidebar':'Buyer sidebar',236,824,C.deep,0,'VERTICAL',22,15);
  label(s,admin?'OPERATIONS':'MY SPACE',11,'Extra Bold',C.mint);
  const items=admin?['Overview','Users','Series & packages','Draw & results','Payments','Withdrawals','Disputes','Revenue','Marketing','Audit']:
    ['Dashboard','My tickets','Orders','Payments','Wallet','Withdrawals','Referrals','Results'];
  for(const item of items){const selected=item===active;const row=box(s,'Nav / '+item,192,40,selected?C.purple:C.deep,10,'HORIZONTAL',10,0);row.counterAxisAlignItems='CENTER';label(row,item,13,selected?'Bold':'Medium',selected?C.white:'#D4D2EC');}
  return s;
}
function titleBlock(parent,kicker,title,subtitle) {
  label(parent,kicker.toUpperCase(),11,'Extra Bold',C.purple);
  label(parent,title,34,'Extra Bold',C.ink);
  label(parent,subtitle,14,'Regular',C.muted);
}
function stat(parent,title,value,hint,w=285) {
  const c=box(parent,'Metric / '+title,w,138,C.white,18,'VERTICAL',20,9);
  label(c,title,13,'Medium',C.muted);label(c,value,32,'Extra Bold',C.ink);label(c,hint,11,'Regular',C.muted);return c;
}
function info(parent,title,body,w=500,h=155,bg=C.white) {
  const c=box(parent,'Panel / '+title,w,h,bg,18,'VERTICAL',20,12);
  label(c,title,19,'Bold',C.ink);label(c,body,13,'Regular',C.muted,w-42);return c;
}
function pageBody(parent,active,admin=false) {
  const row=box(parent,'Main layout',1440,824,C.canvas,0,'HORIZONTAL',0,0);side(row,active,admin);
  return box(row,'Content',1204,824,C.canvas,0,'VERTICAL',35,18);
}
function dashboard(index) {
  const root=screen('dashboard','User dashboard',index);header(root,'My account');const main=pageBody(root,'Dashboard');
  titleBlock(main,'Your account','Good morning, Rohit','Your tickets, orders and wallet status in one clear place.');
  const stats=box(main,'Summary cards',1134,139,null,0,'HORIZONTAL',0,16);
  stat(stats,'My tickets','04','Delivered tickets',274);stat(stats,'Open orders','01','Awaiting server status',274);stat(stats,'Available wallet','INR 0','Sample balance',274);stat(stats,'Held wallet','INR 0','Sample balance',274);
  const hero=box(main,'Next action',1134,176,C.deep,19,'HORIZONTAL',26,30);
  const copy=box(hero,'Next action copy',760,120,null,0,'VERTICAL',0,10);label(copy,'CONTINUE YOUR JOURNEY',11,'Extra Bold',C.mint);label(copy,'Explore the next series',27,'Bold',C.white);label(copy,'Choose tickets, review the server price, then follow payment status.',13,'Regular','#D8D6EF',690);
  button(hero,'Explore series');
  const row=box(main,'Orders and tickets',1134,287,null,0,'HORIZONTAL',0,16);
  const orders=box(row,'Recent orders',559,287,C.white,18,'VERTICAL',22,14);label(orders,'Recent orders',20,'Bold');tag(orders,'PENDING PAYMENT',C.sand,C.red);label(orders,'Sample order  ·  INR 250',16,'Semi Bold');label(orders,'Continue checkout to see the method and latest status.',13,'Regular',C.muted,500);button(orders,'View order',true);
  const tickets=box(row,'Tickets overview',559,287,C.white,18,'VERTICAL',22,14);label(tickets,'My tickets',20,'Bold');tag(tickets,'RESULT PENDING',C.lilac,C.purple);label(tickets,'Ticket serials appear after confirmed delivery.',15,'Semi Bold',C.ink,500);label(tickets,'A ticket is not a win until the published result confirms it.',13,'Regular',C.muted,500);button(tickets,'View tickets',true);
  return root;
}
function packages(index) {
  const root=screen('packages','Packages catalog',index);header(root,'Packages');
  const main=box(root,'Catalog content',1440,824,C.canvas,0,'VERTICAL',42,20);
  titleBlock(main,'Ticket collections','Find a package that fits','Every package shows its included series, price and availability before checkout.');
  const strip=box(main,'Catalog filters',1356,62,C.white,15,'HORIZONTAL',18,24);strip.counterAxisAlignItems='CENTER';
  tag(strip,'ALL PACKAGES',C.deep,C.white);label(strip,'Available',14,'Semi Bold');label(strip,'By price',14,'Medium',C.muted);label(strip,'By included series',14,'Medium',C.muted);
  const grid=box(main,'Package cards',1356,441,null,0,'HORIZONTAL',0,18);
  const examples=[['Starter Mix','2 series  ·  3 tickets','INR 250','A compact introduction to current series.'],['Explorer Bundle','3 series  ·  6 tickets','INR 500','A broader mix of available ticket series.'],['Choose your series','See live packages','Server price','Only active packages are purchasable.']];
  for(const [name,detail,price,description] of examples){const c=box(grid,'Package / '+name,440,441,C.white,21,'VERTICAL',22,15);const art=box(c,'Package artwork',396,145,C.lilac,15,'VERTICAL',17,8);tag(art,'PACKAGE',C.deep,C.white);label(art,'✦  ✦  ✦',33,'Bold',C.purple);label(c,name,24,'Extra Bold');label(c,detail,13,'Semi Bold',C.purple);label(c,description,13,'Regular',C.muted,390);label(c,price,26,'Extra Bold');button(c,'View package',true);}
  const note=box(main,'Catalog note',1356,91,C.sand,14,'VERTICAL',17,6);label(note,'Availability is checked again when an order is reserved.',14,'Bold');label(note,'These cards use illustrative titles and prices; live catalog data comes from the server.',12,'Regular',C.muted);
  return root;
}
function tickets(index) {
  const root=screen('tickets','Scratch-card style ticket',index);header(root,'My account');const main=pageBody(root,'My tickets');
  titleBlock(main,'Ticket collection','My tickets','A visual ticket card with transparent result status — no scratch-to-reveal action.');
  const row=box(main,'Ticket and status',1134,437,null,0,'HORIZONTAL',0,18);
  const left=box(row,'Ticket artwork',540,437,C.deep,22,'VERTICAL',28,21);tag(left,'SAMPLE TICKET',C.mint,C.mintInk);
  label(left,'GOLDEN SERIES',31,'Extra Bold',C.white);label(left,'SERIAL  •  000127',22,'Bold',C.mint);space(left,1,18);
  const panel=box(left,'Scratch-card pattern',484,156,C.purple,16,'VERTICAL',19,11);label(panel,'✦  ✦  ✦  ✦  ✦',32,'Bold',C.mint);label(panel,'Result status appears after publication',16,'Semi Bold',C.white,430);
  label(left,'Decorative ticket surface · not an instant reveal game',12,'Regular','#DEDDF0',470);
  const right=box(row,'Ticket information',576,437,C.white,22,'VERTICAL',24,16);label(right,'Ticket details',22,'Bold');
  tag(right,'RESULT PENDING',C.lilac,C.purple);label(right,'Series  Golden Series',15,'Semi Bold');label(right,'Order  sample-order-id',13,'Regular',C.muted);label(right,'Draw status  Await published result',13,'Regular',C.muted);
  label(right,'Winning status is shown only after the server publishes an audited result.',13,'Regular',C.muted,520);button(right,'View public results',true);
  const proof=box(main,'Fairness note',1134,121,C.sand,16,'VERTICAL',18,8);label(proof,'Result transparency',17,'Bold');label(proof,'The public result page exposes a commitment, revealed seed and reproducible winner evidence. A card gesture never changes the outcome.',13,'Regular',C.muted,1080);
  return root;
}
function payments(index) {
  const root=screen('payments','Payment checkout',index);header(root,'My account');const main=pageBody(root,'Payments');
  titleBlock(main,'Choose → Review → Pay','Choose payment path','Server-priced order. The method starts a process — it does not mark the order paid.');
  const row=box(main,'Payment workspace',1134,548,null,0,'HORIZONTAL',0,18);
  const methods=box(row,'Payment methods',705,548,C.white,20,'VERTICAL',22,13);label(methods,'Available methods',22,'Bold');
  const entries=[['Manual UPI','Proof goes to independent review','CONFIGURATION REQUIRED'],['P2P exact match','Exact-amount eligible match only','MATCH REQUIRED'],['Telegram Stars','Configured bot must send an invoice','BOT REQUIRED'],['White-label provider','Initiation is not configured','DISABLED']];
  for(const [name,sub,badge] of entries){const c=box(methods,'Method / '+name,661,91,name==='Manual UPI'?C.lilac:C.canvas,13,'HORIZONTAL',14,15);const copy=box(c,'Method copy',405,63,null,0,'VERTICAL',0,6);label(copy,name,16,'Bold');label(copy,sub,12,'Regular',C.muted,400);tag(c,badge,badge==='DISABLED'?C.blush:C.white,badge==='DISABLED'?C.red:C.purple);}
  label(methods,'Only configured methods can create a real payment attempt.',12,'Regular',C.muted);
  const aside=box(row,'Order and safety',411,548,C.white,20,'VERTICAL',22,17);label(aside,'Order summary',22,'Bold');tag(aside,'SAMPLE DATA',C.sand,C.red);label(aside,'Illustrative ticket  ×  1',15,'Medium');label(aside,'Total  INR 250',27,'Extra Bold');
  const safety=box(aside,'Safety note',367,134,C.mint,13,'VERTICAL',15,9);label(safety,'Never pay from a demo screen',15,'Bold',C.mintInk);label(safety,'Payee details appear only in a server-created attempt. UTR or screenshot is not settlement.',12,'Regular',C.mintInk,335);
  button(aside,'Continue to method');label(aside,'No payment is initiated by this Figma design.',11,'Regular',C.muted,350);
  return root;
}
function withdrawals(index) {
  const root=screen('withdrawals','Withdrawal request',index);header(root,'My account');const main=pageBody(root,'Withdrawals');
  titleBlock(main,'Wallet → Eligibility → Match','Withdrawals','See the amount cap, verified destination and locked-funds status before you request.');
  const metrics=box(main,'Withdrawal balances',1134,138,null,0,'HORIZONTAL',0,16);stat(metrics,'Available','INR 0','Sample balance',360);stat(metrics,'Held','INR 0','Not paid out',360);stat(metrics,'Eligible maximum','Server rule','50% floor and limits',382);
  const row=box(main,'Withdrawal form and history',1134,467,null,0,'HORIZONTAL',0,17);
  const form=box(row,'Request form',555,467,C.white,20,'VERTICAL',23,14);label(form,'New withdrawal request',21,'Bold');field(form,'Amount in rupees','Enter an amount');field(form,'Verified destination','Operations-verified destination');
  const rule=box(form,'Eligibility help',509,84,C.sand,12,'VERTICAL',12,6);label(rule,'Server eligibility applies',13,'Bold');label(rule,'One unresolved request per user; exact matching in V1.',12,'Regular',C.muted,480);button(form,'Request withdrawal');
  const history=box(row,'Request history',562,467,C.white,20,'VERTICAL',23,15);label(history,'Request status',21,'Bold');tag(history,'NO REQUESTS',C.lilac,C.purple);label(history,'A submitted request first places a hold.',16,'Semi Bold');label(history,'Waiting for buyer → Matched → Under review / Settled',13,'Regular',C.muted,510);
  const warning=box(history,'Withdrawal warning',516,132,C.blush,12,'VERTICAL',15,9);label(warning,'Payment details exposed?',14,'Bold',C.red);label(warning,'Do not auto-release held funds. Late or uncertain payments stay in review.',12,'Regular',C.red,480);button(history,'View wallet activity',true);
  return root;
}
function auth(index,signup=false) {
  const title=signup?'Create account':'Sign in';const root=screen(signup?'signup':'login',title,index);
  const row=box(root,'Auth split layout',1440,900,C.white,0,'HORIZONTAL',0,0);
  const art=box(row,'Auth story',650,900,C.deep,0,'VERTICAL',62,30);tag(art,'E-TICKET PLATFORM',C.purple,C.white);space(art,1,70);
  label(art,signup?'Start with a clear ticket journey.':'Welcome back to your tickets.',48,'Extra Bold',C.white,510);
  label(art,'Browse series, review orders and follow the published result with full status visibility.',20,'Regular','#D9D7F0',500);
  const shape=box(art,'Illustrative ticket',520,200,C.purple,24,'VERTICAL',26,16);label(shape,'✦    ✦    ✦',35,'Bold',C.mint);label(shape,'YOUR TICKET JOURNEY',18,'Extra Bold',C.white);label(shape,'Design preview · no purchase or win implied',12,'Regular','#E5E3F4');
  const form=box(row,'Auth form',790,900,C.white,0,'VERTICAL',130,22);label(form,signup?'CREATE YOUR ACCOUNT':'WELCOME BACK',12,'Extra Bold',C.purple);label(form,title,38,'Extra Bold');
  label(form,signup?'Use your name and email to create an account.':'Use your email and password to access your dashboard.',15,'Regular',C.muted,490);
  if(signup) field(form,'Full name','Your full name');field(form,'Email','you@example.com');field(form,'Password',signup?'At least 12 characters':'Your password');
  button(form,signup?'Create account':'Sign in');label(form,signup?'Already registered? Sign in':'New here? Create an account',14,'Semi Bold',C.purple);
  const notice=box(form,'Auth reassurance',530,68,C.sand,12,'VERTICAL',12,0);label(notice,'No payment or ticket is created during sign-in or registration.',12,'Medium',C.muted,500);
  return root;
}
function adminOverview(index) {
  const root=screen('admin','Admin overview',index);header(root,'My account');const main=pageBody(root,'Overview',true);
  titleBlock(main,'Operations command center','Platform overview','Server-recorded counts support review; they are not payment verification.');
  const stats=box(main,'Admin KPI cards',1134,139,null,0,'HORIZONTAL',0,16);stat(stats,'Open series','0','Sample empty state',274);stat(stats,'Pending orders','0','Sample empty state',274);stat(stats,'Manual proofs','0','Needs independent review',274);stat(stats,'Open disputes','0','Sample empty state',274);
  const alert=box(main,'Admin safety notice',1134,95,C.sand,16,'VERTICAL',17,7);label(alert,'Evidence is not settlement',16,'Bold');label(alert,'A UTR, screenshot or receiver statement must not silently approve a payment. All decisions need audited evidence.',13,'Regular',C.muted,1080);
  const row=box(main,'Admin operations cards',1134,311,null,0,'HORIZONTAL',0,16);
  info(row,'Series & packages','Catalog lifecycle, inventory, pricing, prizes and package availability.',365,311);
  info(row,'Payment and withdrawals','Manual UPI review, P2P matches, disputes and held funds.',365,311);
  info(row,'Revenue & marketing','Allocated buckets, coupons and pending-review reward records.',372,311);
  return root;
}
function adminModules(index) {
  const root=screen('adminmodules','Admin functions and criteria',index);header(root,'My account');const main=pageBody(root,'Payments',true);
  titleBlock(main,'Review center','Admin sections & criteria','Every action is permissioned, audited and constrained by the backend state machine.');
  const columns=box(main,'Admin module grid',1134,571,null,0,'HORIZONTAL',0,16);
  const left=box(columns,'Admin modules left',559,571,null,0,'VERTICAL',0,13);
  const right=box(columns,'Admin modules right',559,571,null,0,'VERTICAL',0,13);
  const modules=[
    [left,'Users / access','Admin role gates and redacted audit metadata.'],
    [left,'Series / packages','Draft → publish → open → close; inventory and frozen snapshots.'],
    [left,'Draw / results','Commit → close → draw → post prizes → publish; reproducible proof.'],
    [left,'Payments','Manual evidence review; signed provider/Telegram events only.'],
    [right,'Withdrawals / P2P','50% cap, one unresolved request, exact match and safe holds.'],
    [right,'Disputes / refunds','Evidence-backed review; no automatic payout or silent reversal.'],
    [right,'Revenue','Integer-paise balanced allocation and read-only reports.'],
    [right,'Marketing / audit','Coupons and pending rewards; no automatic reward credit.'],
  ];
  for(const [parent,title,copy] of modules){const c=box(parent,'Module / '+title,559,132,C.white,15,'VERTICAL',18,10);label(c,title,17,'Bold');label(c,copy,12,'Regular',C.muted,510);}
  return root;
}
const build={dashboard,packages,tickets,payments,withdrawals,login:i=>auth(i,false),signup:i=>auth(i,true),admin:adminOverview,adminmodules:adminModules};
const names=['dashboard','packages','tickets','payments','withdrawals','login','signup','admin','adminmodules'];
const completed=[];for(const key of WHICH){const index=names.indexOf(key);if(index<0)throw new Error('Unknown screen '+key);const node=build[key](index);completed.push({key,id:node.id,x:node.x,y:node.y,width:node.width,height:node.height});}
return {fileKey:figma.fileKey,pageId:figma.currentPage.id,completed,createdNodeIds,count:createdNodeIds.length};
