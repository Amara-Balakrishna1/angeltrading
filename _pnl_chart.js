let pnlChartView='trades';
function drawPnlChartCore(s,svgId,pillsId,isExpanded){
  const el=document.getElementById(svgId);
  if(!el)return;
  const pillsEl=document.getElementById(pillsId);
  const completed=[],positions=[];
  let revTotal=0,followTotal=0,openTotal=0;
  ['REVERSE','NO-REVERSE'].forEach(mode=>{
    const list=((s.modes&&s.modes[mode]&&s.modes[mode].completed)||[]);
    list.forEach(t=>{
      const p=Number(t.pnl||0);
      if(mode==='REVERSE')revTotal+=p;else followTotal+=p;
      completed.push({...t,mode,closed:true,pnl:p,when:t.exit_time||t.entry_time});
    });
  });
  (s.positions||[]).forEach((p,idx)=>{
    const pnlVal=Number(p.pnl||0);
    openTotal+=pnlVal;
    positions.push({...p,posIndex:idx+1,closed:false,pnl:pnlVal,when:p.time||p.entry_time});
  });
  const netRealized=revTotal+followTotal;
  if(pillsEl){
    const openClass=openTotal>=0?'':'neg';
    pillsEl.innerHTML=`<span class="p-pill p-pill-rev" title="REVERSE booked P&amp;L">&bull; REVERSE: Rs. ${fmt(revTotal)} (${completed.filter(t=>t.mode==='REVERSE').length})</span><span class="p-pill p-pill-follow" title="NO-REVERSE booked P&amp;L">&bull; NO-REVERSE: Rs. ${fmt(followTotal)} (${completed.filter(t=>t.mode==='NO-REVERSE').length})</span><span class="p-pill p-pill-realized ${netRealized>=0?'pos':''}" title="Booked profit or loss of closed trades">Net: Rs. ${fmt(netRealized)}</span>${positions.length?`<span class="p-pill p-pill-open ${openClass}" title="Open positions are not in the net">Open: Rs. ${fmt(openTotal)} (${positions.length})</span>`:''}`;
  }
  if(document.querySelectorAll){
    const subText=pnlChartView==='total'
      ?'Each step is the booked day total after that trade. A dashed step is still open.'
      :'Bars start at zero. The figure under each trade is the booked day total so far.';
    document.querySelectorAll('.pnl-chart-sub').forEach(node=>{node.textContent=subText;});
  }
  const rect=el.getBoundingClientRect?el.getBoundingClientRect():{width:0,height:0};
  const W=Math.max(320,rect.width||(isExpanded?1000:640));
  const H=Math.max(180,rect.height||(isExpanded?500:250));
  el.setAttribute('viewBox',`0 0 ${W} ${H}`);
  el.setAttribute('preserveAspectRatio','xMidYMid meet');
  const r=n=>Math.round(n*10)/10;
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const moneyShort=v=>{
    const neg=v<0,a=Math.abs(v);
    let body;
    if(a>=100000) body=(a/100000).toFixed(1).replace(/\.0$/,'')+'L';
    else if(a>=1000) body=(a/1000).toFixed(1).replace(/\.0$/,'')+'k';
    else body=String(Math.round(a));
    return (neg?'-':'')+body;
  };
  const signedShort=v=>v>0?'+'+moneyShort(v):moneyShort(v);
  const niceStep=(span,ticks)=>{
    if(!(span>0)||!isFinite(span)) return 1000;
    const raw=span/Math.max(1,ticks);
    const pow=Math.pow(10,Math.floor(Math.log10(raw)));
    if(!(pow>0)) return 1;
    const err=raw/pow;
    const nice=err>=7.5?10:err>=3.5?5:err>=1.5?2:1;
    return nice*pow;
  };
  const toMin=t=>{const m=/^(\d{1,2}):(\d{2})/.exec(t||'');return m?Number(m[1])*60+Number(m[2]):null;};
  const modeColor=m=>m==='REVERSE'?'var(--purple)':m==='NO-REVERSE'?'var(--blue)':'var(--muted)';
  const pnlColor=v=>v>=0?'var(--green)':'var(--red)';
  const prettyReason=raw=>{
    const reason=String(raw||'').replace(/^EXIT_/,'').replace(/_/g,' ').toLowerCase();
    return reason?reason.charAt(0).toUpperCase()+reason.slice(1,22):'';
  };
  completed.forEach(t=>{t.exitMin=toMin(t.exit_time||t.when);t.entryMin=toMin(t.entry_time);});
  completed.sort((a,b)=>{
    const ae=a.exitMin==null?1e9:a.exitMin, be=b.exitMin==null?1e9:b.exitMin;
    if(ae!==be) return ae-be;
    return String(a.entry_time||'').localeCompare(String(b.entry_time||''));
  });
  let cum=0,revCum=0,followCum=0;
  completed.forEach((t,i)=>{
    cum+=t.pnl;
    t.cumPnl=cum;
    t.tradeIndex=i+1;
    t.label='T'+t.tradeIndex;
    if(t.mode==='REVERSE'){revCum+=t.pnl;t.modeCum=revCum;}
    else {followCum+=t.pnl;t.modeCum=followCum;}
  });
  if(!completed.length&&!positions.length){
    el.innerHTML=`<text x="${r(W/2)}" y="${r(H/2)}" fill="var(--axis)" text-anchor="middle" font-size="13">No closed trades or open positions yet today</text>`;
    return;
  }
  const padL=isExpanded?76:54;
  const padR=isExpanded?28:18;
  const padB=isExpanded?78:46;
  const padT=isExpanded?32:24;
  const chartW=Math.max(40,W-padL-padR);
  const chartH=Math.max(40,H-padT-padB);
  const bottom=H-padB;
  const axisTitle=pnlChartView==='total'?'Day total (Rs.)':'This trade (Rs.)';
  function scale(vals){
    let lo=Math.min(0,...vals), hi=Math.max(0,...vals);
    if(!isFinite(lo)||!isFinite(hi)){lo=-1;hi=1;}
    if(lo===hi){lo-=1;hi+=1;}
    const span=hi-lo;
    const step=niceStep(span,4);
    let yLo=Math.floor(lo/step)*step;
    let yHi=Math.ceil(hi/step)*step;
    if(yLo===yHi){yLo-=step;yHi+=step;}
    if(hi>0 && (yHi-hi)<span*0.14) yHi+=step;
    if(lo<0 && (lo-yLo)<span*0.14) yLo-=step;
    const Y=v=>padT+(yHi-v)/(yHi-yLo)*chartH;
    return {yLo,yHi,step,Y};
  }
  function grid(sc){
    const parts=[];
    const tickCount=Math.max(1,Math.round((sc.yHi-sc.yLo)/sc.step));
    for(let i=0;i<=tickCount;i++){
      const v=Math.round((sc.yLo+i*sc.step)*100)/100;
      if(Math.abs(v)<=sc.step*0.001) continue;
      const y=sc.Y(v);
      if(y<padT-1||y>bottom+1) continue;
      parts.push(`<line pointer-events="none" x1="${padL}" x2="${r(W-padR)}" y1="${r(y)}" y2="${r(y)}" stroke="var(--grid)" stroke-dasharray="3 3"/>`);
      parts.push(`<text pointer-events="none" x="${padL-8}" y="${r(y+4)}" fill="var(--axis)" font-size="${isExpanded?12:10}" text-anchor="end">${moneyShort(v)}</text>`);
    }
    return parts.join('');
  }
  function zeroLine(sc){
    const y=sc.Y(0);
    return `<line data-kind="zero" data-zero="${r(y)}" pointer-events="none" x1="${padL}" x2="${r(W-padR)}" y1="${r(y)}" y2="${r(y)}" stroke="var(--zero)" stroke-width="1.4"/><text pointer-events="none" x="${padL-8}" y="${r(y+4)}" fill="var(--zero)" font-size="${isExpanded?12:10}" font-weight="700" text-anchor="end">0</text>`;
  }
  function title(){
    return `<text pointer-events="none" x="${padL}" y="14" fill="var(--axis)" font-size="11" font-weight="600">${axisTitle}</text>`;
  }
  function bindTrades(list){
    el.querySelectorAll('.trade-hit').forEach(hit=>{
      const idx=Number(hit.getAttribute('data-trade-idx'));
      const t=list[idx];
      if(!t)return;
      hit.addEventListener('mouseenter',e=>showTradeTooltip(e,t,true));
      hit.addEventListener('mousemove',e=>moveTradeTooltip(e));
      hit.addEventListener('mouseleave',()=>hideTradeTooltip());
    });
  }
  function bindOpens(list){
    el.querySelectorAll('.pos-hit').forEach(hit=>{
      const idx=Number(hit.getAttribute('data-pos-idx'));
      const p=list[idx];
      if(!p)return;
      hit.addEventListener('mouseenter',e=>showTradeTooltip(e,p,false));
      hit.addEventListener('mousemove',e=>moveTradeTooltip(e));
      hit.addEventListener('mouseleave',()=>hideTradeTooltip());
    });
  }
  function xLabel(x,primary,primaryColor,time){
    return `<text pointer-events="none" x="${r(x)}" y="${r(bottom+15)}" text-anchor="middle" font-size="${isExpanded?12:11}"><tspan fill="${primaryColor}" font-weight="700">${esc(primary)}</tspan><tspan fill="var(--axis)" font-weight="500">${time?' · '+esc(time):''}</tspan></text>`;
  }

  if(pnlChartView==='total'){
    const n=completed.length+(positions.length?1:0);
    const slot=chartW/Math.max(1,n);
    const X=i=>padL+(i+0.5)*slot;
    const vals=completed.map(t=>t.cumPnl);
    if(positions.length) vals.push(cum+openTotal);
    const sc=scale(vals);
    const parts=[title(),grid(sc)];
    let prevY=sc.Y(0);
    let d=`M ${r(padL)} ${r(prevY)}`;
    completed.forEach((t,i)=>{
      const x=X(i), y=sc.Y(t.cumPnl);
      d+=` L ${r(x)} ${r(prevY)} L ${r(x)} ${r(y)}`;
      prevY=y;
    });
    if(completed.length){
      parts.push(`<path data-kind="run" pointer-events="none" d="${d}" fill="none" stroke="var(--run)" stroke-width="${isExpanded?2.6:2.2}" stroke-linejoin="round" stroke-linecap="round"/>`);
    }
    completed.forEach((t,i)=>{
      const x=X(i), y=sc.Y(t.cumPnl);
      const col=pnlColor(t.pnl);
      const below=t.pnl<0;
      const ly=below?y+16:y-10;
      const isLast=i===completed.length-1;
      const tag=(isLast?'booked ':'')+signedShort(t.cumPnl);
      const time=String(t.exit_time||'').slice(0,5);
      const reason=isExpanded?prettyReason(t.exit_reason):'';
      parts.push(`<g class="trade-node">`);
      parts.push(`<circle data-kind="dot" data-cum="${t.cumPnl}" pointer-events="none" cx="${r(x)}" cy="${r(y)}" r="${isExpanded?7:6}" fill="${col}" stroke="${modeColor(t.mode)}" stroke-width="2"/>`);
      parts.push(`<text pointer-events="none" data-kind="cum-label" x="${r(x)}" y="${r(ly)}" fill="${col}" font-size="${isExpanded?13:11}" font-weight="700" text-anchor="middle">${esc(tag)}</text>`);
      parts.push(xLabel(x,t.label,modeColor(t.mode),time));
      if(reason) parts.push(`<text pointer-events="none" x="${r(x)}" y="${r(bottom+32)}" fill="var(--axis)" font-size="10" text-anchor="middle">${esc(reason)}</text>`);
      parts.push(`<rect x="${r(x-slot/2)}" y="${padT}" width="${r(slot)}" height="${r(chartH)}" fill="transparent" class="trade-hit" data-trade-idx="${i}" style="cursor:pointer"/>`);
      parts.push(`</g>`);
    });
    if(positions.length){
      const x=X(completed.length);
      const fromX=completed.length?X(completed.length-1):padL;
      const yStart=sc.Y(cum);
      const y=sc.Y(cum+openTotal);
      const col=pnlColor(openTotal);
      const ly=openTotal>=0?y-10:y+16;
      parts.push(`<path data-kind="open-step" pointer-events="none" d="M ${r(fromX)} ${r(yStart)} L ${r(x)} ${r(yStart)} L ${r(x)} ${r(y)}" fill="none" stroke="${col}" stroke-width="2" stroke-dasharray="5 4"/>`);
      parts.push(`<circle data-kind="open-dot" pointer-events="none" cx="${r(x)}" cy="${r(y)}" r="${isExpanded?7:6}" fill="var(--card)" stroke="${col}" stroke-width="2"/>`);
      parts.push(`<text pointer-events="none" x="${r(x)}" y="${r(ly)}" fill="${col}" font-size="${isExpanded?13:11}" font-weight="700" text-anchor="middle">${signedShort(openTotal)}</text>`);
      parts.push(xLabel(x,'OPEN','var(--ink)',positions.length+' live'));
      parts.push(`<text pointer-events="none" x="${r(x)}" y="${r(bottom+30)}" fill="var(--axis)" font-size="10" text-anchor="middle">not booked</text>`);
      parts.push(`<rect x="${r(x-slot/2)}" y="${padT}" width="${r(slot)}" height="${r(chartH)}" fill="transparent" class="pos-hit" data-pos-idx="0" style="cursor:pointer"/>`);
    }
    parts.push(zeroLine(sc));
    el.innerHTML=parts.join('');
    bindTrades(completed);
    el.querySelectorAll('.pos-hit').forEach(hit=>{
      const payload=positions.length===1?positions[0]:{
        mode:'OPEN',pnl:openTotal,posIndex:positions.length,peak:null,
        entry_time:positions.map(p=>p.entry_time).filter(Boolean)[0]||'',
        time:positions.map(p=>p.time).filter(Boolean).slice(-1)[0]||'',
        buy_symbol:positions.map(p=>`${p.mode||'OPEN'} ${p.buy_symbol||''}`.trim()).join(' | '),
        sell_symbol:'N/A',quantity:'',execution_price:'',
        trade_reason:positions.map(p=>`${p.mode||'OPEN'} Rs. ${fmt(p.pnl)}`).join(' · ')
      };
      hit.addEventListener('mouseenter',e=>showTradeTooltip(e,payload,false));
      hit.addEventListener('mousemove',e=>moveTradeTooltip(e));
      hit.addEventListener('mouseleave',()=>hideTradeTooltip());
    });
    return;
  }

  const slots=completed.map(t=>({kind:'trade',t}));
  positions.forEach(p=>slots.push({kind:'open',p}));
  const n=slots.length;
  const slot=chartW/n;
  const barW=Math.max(8,Math.min(isExpanded?56:34,slot*0.58));
  const X=i=>padL+(i+0.5)*slot;
  const sc=scale([...completed.map(t=>t.pnl),...positions.map(p=>p.pnl)]);
  const zeroY=sc.Y(0);
  const parts=[title(),grid(sc)];
  slots.forEach((st,i)=>{
    const x=X(i);
    if(st.kind==='trade'){
      const t=st.t;
      const yEnd=sc.Y(t.pnl);
      const top=Math.min(zeroY,yEnd);
      const h=Math.max(t.pnl===0?2:3,Math.abs(yEnd-zeroY));
      const ly=t.pnl>=0?yEnd-8:yEnd+13;
      const time=String(t.exit_time||'').slice(0,5);
      const wide=slot>150;
      const dayText=wide?('day '+(isExpanded?fmt(t.cumPnl):moneyShort(t.cumPnl))):moneyShort(t.cumPnl);
      const reason=isExpanded?prettyReason(t.exit_reason):'';
      parts.push(`<g class="trade-node">`);
      parts.push(`<rect pointer-events="none" data-kind="bar" data-pnl="${t.pnl}" x="${r(x-barW/2)}" y="${r(top)}" width="${r(barW)}" height="${r(h)}" rx="4" fill="${pnlColor(t.pnl)}"/>`);
      parts.push(`<text pointer-events="none" x="${r(x)}" y="${r(ly)}" fill="${pnlColor(t.pnl)}" font-size="${isExpanded?13:11}" font-weight="700" text-anchor="middle">${signedShort(t.pnl)}</text>`);
      parts.push(xLabel(x,t.label,modeColor(t.mode),time));
      parts.push(`<text pointer-events="none" data-kind="day" x="${r(x)}" y="${r(bottom+29)}" fill="${pnlColor(t.cumPnl)}" font-size="${isExpanded?12:10}" font-weight="700" text-anchor="middle">${esc(dayText)}</text>`);
      if(reason) parts.push(`<text pointer-events="none" x="${r(x)}" y="${r(bottom+44)}" fill="var(--axis)" font-size="10" text-anchor="middle">${esc(reason)}</text>`);
      parts.push(`<rect x="${r(x-slot/2)}" y="${padT}" width="${r(slot)}" height="${r(chartH)}" fill="transparent" class="trade-hit" data-trade-idx="${t.tradeIndex-1}" style="cursor:pointer"/>`);
      parts.push(`</g>`);
    }else{
      const p=st.p;
      const yEnd=sc.Y(p.pnl);
      const top=Math.min(zeroY,yEnd);
      const h=Math.max(p.pnl===0?2:3,Math.abs(yEnd-zeroY));
      const ly=p.pnl>=0?yEnd-8:yEnd+13;
      const time=String(p.time||p.entry_time||'').slice(0,5);
      parts.push(`<g class="pos-node">`);
      parts.push(`<rect pointer-events="none" data-kind="open-bar" data-pnl="${p.pnl}" x="${r(x-barW/2)}" y="${r(top)}" width="${r(barW)}" height="${r(h)}" rx="4" fill="${pnlColor(p.pnl)}" fill-opacity="0.18" stroke="${modeColor(p.mode)}" stroke-width="1.6" stroke-dasharray="4 3"/>`);
      parts.push(`<text pointer-events="none" x="${r(x)}" y="${r(ly)}" fill="${pnlColor(p.pnl)}" font-size="${isExpanded?13:11}" font-weight="700" text-anchor="middle">${signedShort(p.pnl)}</text>`);
      parts.push(xLabel(x,'LIVE',modeColor(p.mode),time));
      parts.push(`<text pointer-events="none" x="${r(x)}" y="${r(bottom+29)}" fill="var(--axis)" font-size="10" text-anchor="middle">not booked</text>`);
      parts.push(`<rect x="${r(x-slot/2)}" y="${padT}" width="${r(slot)}" height="${r(chartH)}" fill="transparent" class="pos-hit" data-pos-idx="${p.posIndex-1}" style="cursor:pointer"/>`);
      parts.push(`</g>`);
    }
  });
  parts.push(zeroLine(sc));
  el.innerHTML=parts.join('');
  bindTrades(completed);
  bindOpens(positions);
}
