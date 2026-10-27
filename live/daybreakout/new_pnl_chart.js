// This is the new, properly formatted P&L chart function
// Replace the minified drawPnlChartCore function with this

function drawPnlChartCore(s, svgId, pillsId, isExpanded) {
    const el = document.getElementById(svgId);
    if (!el) return;
    
    const pillsEl = document.getElementById(pillsId);
    const W = 1000;
    const H = isExpanded ? 500 : 280;
    const view = currentPnlView;
    
    const completed = [];
    const positions = [];
    let revTotal = 0, followTotal = 0, openTotal = 0;
    
    // Process completed trades
    ['REVERSE', 'NO-REVERSE'].forEach(mode => {
        const list = ((s.modes && s.modes[mode] && s.modes[mode].completed) || []);
        list.forEach(t => {
            const p = Number(t.pnl || 0);
            if (mode === 'REVERSE') revTotal += p;
            else followTotal += p;
            completed.push({...t, mode, closed: true, pnl: p, when: t.exit_time || t.entry_time});
        });
    });
    
    // Process positions
    (s.positions || []).forEach((p, idx) => {
        const pnlVal = Number(p.pnl || 0);
        openTotal += pnlVal;
        positions.push({...p, posIndex: idx + 1, closed: false, pnl: pnlVal, when: p.time || p.entry_time});
    });
    
    const netRealized = revTotal + followTotal;
    
    // Update summary pills
    if (pillsEl) {
        pillsEl.innerHTML = `<span class="p-pill p-pill-rev" title="REVERSE Mode Realized P&L">&bull; REVERSE: Rs. ${fmt(revTotal)} (${completed.filter(t => t.mode === 'REVERSE').length})</span><span class="p-pill p-pill-follow" title="NO-REVERSE Mode Realized P&L">&bull; NO-REVERSE: Rs. ${fmt(followTotal)} (${completed.filter(t => t.mode === 'NO-REVERSE').length})</span><span class="p-pill p-pill-realized ${netRealized >= 0 ? 'pos' : ''}" title="Net Realized P&L across all closed trades">Net: Rs. ${fmt(netRealized)}</span>${positions.length ? `<span class="p-pill p-pill-open" title="Open Positions Floating P&L">Live Open: Rs. ${fmt(openTotal)} (${positions.length})</span>` : ''}`;
    }
    
    // Convert times to minutes
    const toMin = t => {
        const m = /^(\d{1,2}):(\d{2})/.exec(t || '');
        return m ? Number(m[1]) * 60 + Number(m[2]) : null;
    };
    
    completed.forEach(t => {
        t.exitMin = toMin(t.exit_time || t.when);
        t.entryMin = toMin(t.entry_time) || t.exitMin;
    });
    
    positions.forEach(p => {
        p.posMin = toMin(p.time || p.when);
        p.entryMin = toMin(p.entry_time) || p.posMin;
    });
    
    // Filter and sort completed trades
    const validCompleted = completed.filter(t => t.exitMin !== null && t.exitMin >= 540 && t.exitMin <= 930);
    validCompleted.sort((a, b) => (a.exitMin || 0) - (b.exitMin || 0));
    
    // Calculate cumulative P&L
    let cum = 0, revCum = 0, followCum = 0;
    validCompleted.forEach((t, i) => {
        cum += t.pnl;
        t.cumPnl = cum;
        t.tradeIndex = i + 1;
        t.label = 'T' + (i + 1);
        if (t.mode === 'REVERSE') revCum += t.pnl;
        else followCum += t.pnl;
        t.modeCum = t.mode === 'REVERSE' ? revCum : followCum;
    });
    
    // Filter positions
    const validPositions = positions.filter(p => p.posMin !== null && p.posMin >= 540 && p.posMin <= 930);
    
    // Handle empty state
    if (!validCompleted.length && !validPositions.length) {
        el.innerHTML = `<text x="${W/2}" y="${H/2}" fill="#94a3b8" text-anchor="middle" font-size="${isExpanded ? 15 : 13}">No live trades or positions recorded today</text>`;
        return;
    }
    
    // Chart dimensions
    const padL = isExpanded ? 80 : 70;
    const padR = 30;
    const padT = isExpanded ? 30 : 25;
    const padB = isExpanded ? 40 : 34;
    const chartW = W - padL - padR;
    const chartH = H - padT - padB;
    const bottom = H - padB;
    
    // Determine Y-axis range
    const allVals = [0, ...validCompleted.map(t => t.pnl), ...validCompleted.map(t => t.cumPnl), ...validPositions.map(p => p.pnl)];
    let lo = Math.min(...allVals);
    let hi = Math.max(...allVals);
    let pad = (hi - lo) * 0.16 || 500;
    lo -= pad;
    hi += pad;
    
    // Coordinate functions
    const X = m => padL + (Math.max(540, Math.min(930, m)) - 540) / 390 * chartW;
    const Y = v => padT + (hi - v) / (hi - lo) * chartH;
    const zeroY = Y(0);
    
    let out = '';
    
    // Draw Y-axis grid
    for (let i = 0; i <= 4; i++) {
        const v = lo + (hi - lo) * i / 4;
        const y = Y(v);
        out += `<line x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}" stroke="#26334c" stroke-dasharray="3 3"/><text x="${padL - 8}" y="${y + 4}" fill="#94a3b8" font-size="${isExpanded ? 11 : 10}" text-anchor="end">Rs. ${fmt(v)}</text>`;
    }
    
    // Draw zero line
    out += `<line x1="${padL}" x2="${W - padR}" y1="${zeroY}" y2="${zeroY}" stroke="#64748b" stroke-width="1.5" stroke-dasharray="4 4"/><text x="${W - padR - 5}" y="${zeroY - 5}" fill="#64748b" font-size="10" font-weight="600" text-anchor="end">Rs. 0.00</text>`;
    
    // Draw X-axis time labels
    for (let m = 540; m <= 930; m += 30) {
        const x = X(m);
        const label = `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
        out += `<line x1="${x}" x2="${x}" y1="${padT}" y2="${bottom}" stroke="#1e293b"/><text x="${x}" y="${bottom + (isExpanded ? 18 : 16)}" fill="#94a3b8" font-size="${isExpanded ? 11 : 10}" text-anchor="middle">${label}</text>`;
    }
    
    // VIEW: EACH TRADE (Bar Chart)
    if (view === 'trades' && validCompleted.length > 0) {
        const barWidth = Math.min(40, Math.max(12, (chartW / Math.max(1, validCompleted.length)) * 0.6));
        const gap = 8;
        const totalWidth = validCompleted.length * (barWidth + gap) - gap;
        const startX = padL + (chartW - totalWidth) / 2;
        
        validCompleted.forEach((t, i) => {
            const x = startX + i * (barWidth + gap);
            const y = Y(t.pnl);
            const h = zeroY - y;
            const isProfit = t.pnl >= 0;
            const stratColor = t.mode === 'REVERSE' ? '#7c3aed' : '#2563eb';
            const pnlColor = isProfit ? '#37d39a' : '#ff657a';
            
            // Main bar (mode color)
            out += `<rect x="${x}" y="${isProfit ? y : zeroY}" width="${barWidth}" height="${Math.abs(h)}" fill="${stratColor}" opacity="0.8" rx="2"/>`;
            // Overlay (profit/loss tint)
            out += `<rect x="${x}" y="${isProfit ? y : zeroY}" width="${barWidth}" height="${Math.abs(h)}" fill="${pnlColor}" opacity="0.3" rx="2"/>`;
            // Trade label badge
            out += `<rect x="${x}" y="${y - 22}" width="${barWidth}" height="20" rx="3" fill="#1e293b" stroke="${stratColor}" stroke-width="1.5"/>`;
            out += `<text x="${x + barWidth / 2}" y="${y - 8}" fill="#e2e8f0" font-size="10" font-weight="700" text-anchor="middle">${t.label}</text>`;
            // P&L value
            const textY = isProfit ? y + 16 : zeroY - 6;
            const shortPnl = (t.pnl >= 0 ? '+' : '') + (Math.abs(t.pnl) >= 1000 ? (t.pnl / 1000).toFixed(1) + 'k' : t.pnl.toFixed(0));
            out += `<text x="${x + barWidth / 2}" y="${textY}" fill="${pnlColor}" font-size="10" font-weight="600" text-anchor="middle">${shortPnl}</text>`;
            // Hit area for tooltip
            out += `<rect x="${x}" y="${y}" width="${barWidth}" height="${Math.abs(h)}" fill="transparent" class="trade-hit" data-trade-idx="${t.tradeIndex - 1}" style="cursor:pointer;"/>`;
        });
    }
    
    // VIEW: DAY TOTAL (Cumulative Line Chart)
    if (view === 'total' && validCompleted.length > 0) {
        // Draw gradient area under cumulative line
        const firstMin = Math.min(validCompleted[0].entryMin || 555, validCompleted[0].exitMin || 555);
        const startX = X(firstMin);
        let pathD = `M ${startX} ${Y(0)}`;
        validCompleted.forEach(t => {
            pathD += ` L ${X(t.exitMin)} ${Y(t.cumPnl)}`;
        });
        const lastX = X(validCompleted[validCompleted.length - 1].exitMin);
        const areaD = `${pathD} L ${lastX} ${zeroY} L ${startX} ${zeroY} Z`;
        
        out += `<defs><linearGradient id="grad-profit-${svgId}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#38bdf8" stop-opacity="0.25"/><stop offset="100%" stop-color="#38bdf8" stop-opacity="0.0"/></linearGradient></defs>`;
        out += `<path d="${areaD}" fill="url(#grad-profit-${svgId})"/>`;
        out += `<path d="${pathD}" fill="none" stroke="#38bdf8" stroke-width="${isExpanded ? 3 : 2.5}" stroke-linecap="round" stroke-linejoin="round"/>`;
        out += `<path d="${pathD}" fill="none" stroke="transparent" stroke-width="16" class="pnl-cum-hover" data-cum-info="Account Realized Profit Movement: Net Rs. ${fmt(validCompleted[validCompleted.length - 1].cumPnl)} across ${validCompleted.length} closed trade(s)." style="cursor:pointer;"/>`;
        
        // Draw mode-specific cumulative lines
        ['REVERSE', 'NO-REVERSE'].forEach(mode => {
            const mTrades = validCompleted.filter(t => t.mode === mode);
            if (mTrades.length > 1) {
                const color = mode === 'REVERSE' ? '#7c3aed' : '#2563eb';
                let stratD = `M ${X(mTrades[0].exitMin)} ${Y(mTrades[0].pnl)}`;
                for (let i = 1; i < mTrades.length; i++) {
                    stratD += ` L ${X(mTrades[i].exitMin)} ${Y(mTrades[i].pnl)}`;
                }
                out += `<path d="${stratD}" fill="none" stroke="${color}" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.8"/>`;
            }
        });
        
        // Draw trade nodes
        validCompleted.forEach(t => {
            const xExit = X(t.exitMin);
            const yPnl = Y(t.pnl);
            const xEntry = X(t.entryMin);
            const isProfit = t.pnl >= 0;
            const stratColor = t.mode === 'REVERSE' ? '#7c3aed' : '#2563eb';
            const pnlColor = isProfit ? '#37d39a' : '#ff657a';
            
            if (Math.abs(xExit - xEntry) > 3) {
                out += `<line x1="${xEntry}" x2="${xExit}" y1="${yPnl}" y2="${yPnl}" stroke="${stratColor}" stroke-width="1.5" stroke-dasharray="3 3" opacity="0.65"/><circle cx="${xEntry}" cy="${yPnl}" r="2.5" fill="${stratColor}" opacity="0.75"/>`;
            }
            
            const nodeR = isExpanded ? 7 : 6;
            out += `<g class="trade-node">`;
            out += `<circle cx="${xExit}" cy="${yPnl}" r="${nodeR + 3}" fill="${stratColor}" opacity="0.25"/>`;
            out += `<circle cx="${xExit}" cy="${yPnl}" r="${nodeR}" fill="#0f172a" stroke="${stratColor}" stroke-width="2"/>`;
            out += `<circle cx="${xExit}" cy="${yPnl}" r="${nodeR - 3.5}" fill="${pnlColor}"/>`;
            
            const badgeY = yPnl - (nodeR + 6);
            out += `<rect x="${xExit - 11}" y="${badgeY - 11}" width="22" height="14" rx="4" fill="#1e293b" stroke="${stratColor}" stroke-width="1"/>`;
            out += `<text x="${xExit}" y="${badgeY}" fill="#e2e8f0" font-size="9" font-weight="700" text-anchor="middle">${t.label}</text>`;
            
            const textY = yPnl + (isProfit ? 18 : -10);
            const shortPnl = (t.pnl >= 0 ? '+' : '') + (Math.abs(t.pnl) >= 1000 ? (t.pnl / 1000).toFixed(1) + 'k' : t.pnl.toFixed(0));
            out += `<text x="${xExit}" y="${textY}" fill="${pnlColor}" font-size="9" font-weight="600" text-anchor="middle">${shortPnl}</text>`;
            
            out += `<circle cx="${xExit}" cy="${yPnl}" r="16" fill="transparent" class="trade-hit" data-trade-idx="${t.tradeIndex - 1}" style="cursor:pointer;"/></g>`;
        });
    }
    
    // Draw open positions (both views)
    validPositions.forEach(p => {
        const xPos = X(p.posMin);
        const yPnl = Y(p.pnl);
        const isProfit = p.pnl >= 0;
        const pnlColor = isProfit ? '#37d39a' : '#ff657a';
        const dimR = isExpanded ? 7 : 6;
        
        out += `<g class="pos-node">`;
        out += `<rect x="${xPos - dimR - 2}" y="${yPnl - dimR - 2}" width="${(dimR + 2) * 2}" height="${(dimR + 2) * 2}" fill="${pnlColor}" opacity="0.2" rx="2" transform="rotate(45 ${xPos} ${yPnl})"/>`;
        out += `<rect x="${xPos - dimR}" y="${yPnl - dimR}" width="${dimR * 2}" height="${dimR * 2}" fill="${pnlColor}" stroke="#ffffff" stroke-width="1.5" rx="2" transform="rotate(45 ${xPos} ${yPnl})"/>`;
        
        const badgeY = yPnl - (dimR + 6);
        out += `<rect x="${xPos - 14}" y="${badgeY - 11}" width="28" height="14" rx="4" fill="#064e3b" stroke="#34d399" stroke-width="1"/>`;
        out += `<text x="${xPos}" y="${badgeY}" fill="#34d399" font-size="8" font-weight="800" text-anchor="middle">LIVE</text>`;
        
        out += `<circle cx="${xPos}" cy="${yPnl}" r="16" fill="transparent" class="pos-hit" data-pos-idx="${p.posIndex - 1}" style="cursor:pointer;"/></g>`;
    });
    
    el.innerHTML = out;
    
    // Add tooltip event listeners for trade nodes
    el.querySelectorAll('.trade-hit').forEach(hit => {
        const idx = Number(hit.getAttribute('data-trade-idx'));
        const t = validCompleted[idx];
        if (!t) return;
        hit.addEventListener('mouseenter', e => showTradeTooltip(e, t, true));
        hit.addEventListener('mousemove', e => moveTradeTooltip(e));
        hit.addEventListener('mouseleave', () => hideTradeTooltip());
    });
    
    // Add tooltip event listeners for position nodes
    el.querySelectorAll('.pos-hit').forEach(hit => {
        const idx = Number(hit.getAttribute('data-pos-idx'));
        const p = validPositions[idx];
        if (!p) return;
        hit.addEventListener('mouseenter', e => showTradeTooltip(e, p, false));
        hit.addEventListener('mousemove', e => moveTradeTooltip(e));
        hit.addEventListener('mouseleave', () => hideTradeTooltip());
    });
    
    // Add tooltip for cumulative line
    el.querySelectorAll('.pnl-cum-hover').forEach(line => {
        line.addEventListener('mouseenter', e => {
            const info = line.getAttribute('data-cum-info');
            showGenericTooltip(e, `<div class="tt-head"><div class="tt-title">Profit Movement</div><div class="tt-badge p-pill-follow">REALIZED</div></div><div class="tt-desc" style="font-size:12px;color:#e2e8f0;margin:0;padding:0;border:none;">${info}</div>`);
        });
        line.addEventListener('mousemove', e => moveTradeTooltip(e));
        line.addEventListener('mouseleave', () => hideTradeTooltip());
    });
}
