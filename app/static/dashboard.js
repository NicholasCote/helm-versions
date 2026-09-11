// Helm Chart Dashboard JavaScript
let refreshInterval;

// Staleness tiers, most -> least severe. Mirrors TIER_ORDER in app/staleness.py.
const TIER_ORDER = ['major', 'stale', 'patch', 'current', 'ahead', 'unknown'];
const TIER_LABELS = {
    major: 'Major',
    stale: 'Stale',
    patch: 'Patch',
    current: 'Up to date',
    ahead: 'Ahead',
    unknown: 'Unknown'
};
// Tiers that mean "there is an upgrade to take"
const NEEDS_UPDATE_TIERS = ['major', 'stale', 'patch'];

// Filter state. 'all' shows everything the API returned.
const CLUSTER_FILTER_KEY = 'helmDashboard.selectedCluster';
const TIER_FILTER_KEY = 'helmDashboard.selectedTier';
let selectedCluster = loadStored(CLUSTER_FILTER_KEY);
let selectedTier = loadStored(TIER_FILTER_KEY);
let latestData = null;  // Cached so switching filters re-renders without a refetch

// Chart objects for the rendered cards, by synthetic id. Cards carry only the id,
// never chart data in a data- attribute or an inline onclick - interpolating chart
// fields into markup is exactly the escaping problem we're avoiding.
const chartIndex = new Map();
let chartCounter = 0;

// Diff buttons are hidden entirely when the container has no helm binary
let helmAvailable = true;

function loadStored(key) {
    try {
        return localStorage.getItem(key) || 'all';
    } catch (e) {
        return 'all';
    }
}

function saveStored(key, value) {
    try {
        localStorage.setItem(key, value);
    } catch (e) {
        // Storage unavailable (private mode, blocked cookies) - filter still works
    }
}

// Everything rendered here goes through innerHTML, and chart names, repo URLs and
// version strings all come from a git repo rather than from this code.
function esc(value) {
    return String(value === null || value === undefined ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// A payload from before staleness tiers existed (cached page, mid-deploy) has no
// staleness_tier, so fall back to the old two-state logic rather than rendering
// an unstyled card.
function tierOf(chart) {
    if (chart.staleness_tier) {
        return chart.staleness_tier;
    }
    if (!chart.latest_version) {
        return 'unknown';
    }
    return chart.needs_update ? 'major' : 'current';
}

function onClusterChange(value) {
    selectedCluster = value;
    saveStored(CLUSTER_FILTER_KEY, value);
    if (latestData) {
        renderData(latestData);
    }
}

function onTierChange(value) {
    selectedTier = value;
    saveStored(TIER_FILTER_KEY, value);
    if (latestData) {
        renderData(latestData);
    }
}

// Rebuild the tier dropdown, annotated with how many charts sit in each tier
function populateTierFilter(summary) {
    const select = document.getElementById('tierFilter');
    if (!select) return;

    const byTier = (summary && summary.by_tier) || {};
    const options = ['<option value="all">All statuses</option>'].concat(
        TIER_ORDER.map(tier =>
            `<option value="${tier}">${TIER_LABELS[tier]} (${byTier[tier] || 0})</option>`)
    );

    select.innerHTML = options.join('');
    select.value = selectedTier;
}

// Rebuild the cluster dropdown to match the clusters actually present in the data
function populateClusterFilter(clusters) {
    const select = document.getElementById('clusterFilter');
    if (!select) return;

    const names = Object.keys(clusters);

    // A remembered cluster may not exist anymore (e.g. CLUSTERS was narrowed)
    if (selectedCluster !== 'all' && !names.includes(selectedCluster)) {
        selectedCluster = 'all';
        saveStored(CLUSTER_FILTER_KEY, selectedCluster);
    }

    const wanted = ['all'].concat(names).join(',');
    if (select.dataset.options !== wanted) {
        select.innerHTML = '<option value="all">All clusters</option>' +
            names.map(name => `<option value="${esc(name)}">${esc(name.toUpperCase())}</option>`).join('');
        select.dataset.options = wanted;
    }

    select.value = selectedCluster;
}

function filterClusters(clusters) {
    if (selectedCluster === 'all' || !clusters[selectedCluster]) {
        return clusters;
    }
    return { [selectedCluster]: clusters[selectedCluster] };
}

// Recursively add up every number in a summary, including nested dicts like
// by_tier and by_source. Generic so a new summary key needs no change here.
function mergeNumeric(target, source) {
    Object.entries(source || {}).forEach(([key, value]) => {
        if (typeof value === 'number') {
            target[key] = (target[key] || 0) + value;
        } else if (value && typeof value === 'object') {
            target[key] = mergeNumeric(target[key] || {}, value);
        }
    });
    return target;
}

// Totals for whatever is currently visible, so the cards match the dropdown
function summarizeClusters(clusters) {
    return Object.values(clusters).reduce(
        (totals, cluster) => mergeNumeric(totals, cluster.summary), {});
}

async function fetchData() {
    try {
        const response = await fetch('/api/charts');
        const data = await response.json();
        return data;
    } catch (error) {
        console.error('Error fetching data:', error);
        return { error: error.message };
    }
}

function formatDate(isoString) {
    if (!isoString) return 'Never';
    return new Date(isoString).toLocaleString();
}

function summaryCard(tier, label, count) {
    const active = selectedTier === tier ? ' active' : '';
    return `
        <div class="summary-card ${tier === 'all' ? 'total' : 'tier-' + tier} clickable${active}"
             data-tier="${tier}" role="button" tabindex="0"
             title="Show only ${label.toLowerCase()}">
            <div class="card-header">${label}</div>
            <div class="card-body">
                <div class="number">${count}</div>
            </div>
        </div>
    `;
}

// Cards show unfiltered totals for the visible clusters - they are the legend and
// the navigation, not a reflection of what the grid is currently showing.
function updateSummaryCards(summary) {
    if (!summary) return;

    const byTier = summary.by_tier || {};
    // 'Ahead' is rare; only give it a card when it actually happens.
    const tiers = TIER_ORDER.filter(tier => tier !== 'ahead' || byTier.ahead);

    const cards = [summaryCard('all', 'Total Charts', summary.total_charts || 0)].concat(
        tiers.map(tier => summaryCard(tier, TIER_LABELS[tier], byTier[tier] || 0))
    );

    document.getElementById('summaryCards').innerHTML = cards.join('');
}

function renderChartCard(chart, chartId) {
    const tier = tierOf(chart);
    const behind = NEEDS_UPDATE_TIERS.includes(tier);

    const versionDisplay = chart.latest_version ? `
        <div class="chart-versions">
            <span class="version current">${esc(chart.current_version)}</span>
            ${behind ? '<span class="arrow"><i class="fas fa-arrow-right"></i></span>' : ''}
            <span class="version latest">${esc(chart.latest_version)}</span>
        </div>
    ` : `
        <div class="chart-versions">
            <span class="version current">${esc(chart.current_version)}</span>
            <span style="color: #999; font-style: italic;">${esc(chart.no_version_reason || 'No version info')}</span>
        </div>
    `;

    const badge = chart.staleness_label
        ? `<span class="staleness-badge tier-${tier}">${esc(chart.staleness_label)}</span>`
        : '';

    const canDiff = helmAvailable && behind && chart.chart_name
        && chart.repo_url && chart.latest_version;
    const diffButton = canDiff
        ? `<button class="diff-btn" type="button" data-chart-id="${esc(chartId)}">
               <i class="fas fa-code-compare"></i> View Diff
           </button>`
        : '';

    return `
        <div class="chart-card tier-${tier}">
            <div class="chart-name"><i class="fas fa-cube"></i> ${esc(chart.name)}${chart.source ? `<span class="chart-source">${esc(chart.source)}</span>` : ''}</div>
            ${versionDisplay}
            ${badge}
            ${chart.repo_url ? `<div class="chart-repo"><i class="fas fa-link"></i> ${esc(chart.repo_url)}</div>` : ''}
            ${diffButton}
        </div>
    `;
}

function renderData(data) {
    latestData = data;

    // Show updating banner if refresh is in progress
    if (data.update_in_progress) {
        const banner = `
            <div class="alert alert-info text-center" style="margin-bottom: 20px; background-color: #007fa3; color: white; border: none; border-radius: 8px;">
                <i class="fas fa-sync-alt fa-spin"></i> <strong>Updating chart data...</strong> This may take a few minutes to complete.
            </div>
        `;
        
        // If we have existing data, show it with the banner
        if (data.data && data.data.clusters) {
            populateClusterFilter(data.data.clusters);
            const visible = filterClusters(data.data.clusters);
            const summary = summarizeClusters(visible);
            populateTierFilter(summary);
            updateSummaryCards(summary);
            document.getElementById('content').innerHTML = banner + generateClustersHtml(visible);
            document.getElementById('lastUpdate').textContent = formatDate(data.last_update);
            return;
        } else {
            // No existing data, just show loading message
            document.getElementById('content').innerHTML = banner + `
                <div class="loading">
                    <div class="spinner"></div>
                    <br><br>
                    Initial chart analysis in progress...
                </div>
            `;
            return;
        }
    }
    
    if (data.error) {
        document.getElementById('content').innerHTML = `
            <div class="error">
                <h4><i class="fas fa-exclamation-triangle"></i> Error loading data:</h4>
                <p>${esc(data.error)}</p>
            </div>
        `;
        return;
    }

    if (!data.data || !data.data.clusters) {
        document.getElementById('content').innerHTML = `
            <div class="error">
                <h4><i class="fas fa-info-circle"></i> No data available</h4>
                <p>Charts data is not yet available. Please try refreshing.</p>
            </div>
        `;
        return;
    }

    populateClusterFilter(data.data.clusters);
    const visible = filterClusters(data.data.clusters);
    const summary = summarizeClusters(visible);
    populateTierFilter(summary);
    updateSummaryCards(summary);
    document.getElementById('content').innerHTML = generateClustersHtml(visible);
    document.getElementById('lastUpdate').textContent = formatDate(data.last_update);
}

function generateClustersHtml(clusters) {
    let html = '';

    // Rebuilt every render, since the ids are positional
    chartIndex.clear();

    if (Object.keys(clusters).length === 0) {
        return `
            <div class="error">
                <h4><i class="fas fa-info-circle"></i> No clusters to show</h4>
                <p>No data for the selected cluster.</p>
            </div>
        `;
    }

    Object.keys(clusters).forEach(clusterName => {
        const cluster = clusters[clusterName];
        const summary = cluster.summary || {};
        const byTier = summary.by_tier || {};

        const charts = (cluster.charts || []).filter(
            chart => selectedTier === 'all' || tierOf(chart) === selectedTier);

        // A cluster with nothing matching the tier filter is hidden entirely, rather
        // than left as an empty header.
        if (charts.length === 0) {
            return;
        }

        const pills = TIER_ORDER
            .filter(tier => byTier[tier])
            .map(tier => `<span class="stat tier-${tier}">${byTier[tier]} ${TIER_LABELS[tier].toLowerCase()}</span>`)
            .join('');

        html += `
            <div class="cluster-section">
                <div class="cluster-header">
                    <div class="cluster-name"><i class="fas fa-server"></i> ${esc(clusterName.toUpperCase())}</div>
                    <div class="cluster-stats">${pills}</div>
                </div>
                <div class="charts-container">
                    <div class="charts-grid">
                        ${charts.map(chart => {
                            const chartId = `c${chartCounter++}`;
                            chartIndex.set(chartId, chart);
                            return renderChartCard(chart, chartId);
                        }).join('')}
                    </div>
                </div>
            </div>
        `;
    });

    if (!html) {
        return `
            <div class="error">
                <h4><i class="fas fa-info-circle"></i> Nothing matches this filter</h4>
                <p>No charts are in the <strong>${esc(TIER_LABELS[selectedTier] || selectedTier)}</strong> tier
                   for the selected cluster. <a href="#" onclick="onTierChange('all'); return false;">Show all statuses</a>.</p>
            </div>
        `;
    }

    return html;
}

async function refreshData() {
    const refreshBtn = document.getElementById('refreshBtn');
    const refreshText = document.getElementById('refreshText');
    
    refreshBtn.disabled = true;
    refreshText.innerHTML = '<span class="spinner"></span> Refreshing...';

    try {
        // Trigger refresh
        await fetch('/api/refresh', { method: 'POST' });
        
        // Poll for updates
        let attempts = 0;
        const maxAttempts = 60; // 5 minutes max
        
        const pollForUpdate = async () => {
            const data = await fetchData();
            
            if (!data.update_in_progress) {
                renderData(data);
                refreshBtn.disabled = false;
                refreshText.innerHTML = '<i class="fas fa-sync-alt"></i> Refresh';
                return;
            }
            
            attempts++;
            if (attempts < maxAttempts) {
                setTimeout(pollForUpdate, 5000); // Poll every 5 seconds
            } else {
                refreshBtn.disabled = false;
                refreshText.innerHTML = '<i class="fas fa-sync-alt"></i> Refresh';
                alert('Refresh timed out. Please try again.');
            }
        };
        
        setTimeout(pollForUpdate, 2000); // Start polling after 2 seconds
        
    } catch (error) {
        console.error('Error refreshing:', error);
        refreshBtn.disabled = false;
        refreshText.innerHTML = '<i class="fas fa-sync-alt"></i> Refresh';
    }
}

// Initial load
async function initializeData() {
    await checkHelmAvailable();
    const data = await fetchData();
    renderData(data);
    
    // If update is in progress, poll for completion
    if (data.update_in_progress) {
        pollForCompletion();
    }
    
    // No automatic refresh - user will manually refresh as needed
}

// Poll for completion when update is in progress
async function pollForCompletion() {
    const pollInterval = setInterval(async () => {
        const data = await fetchData();
        renderData(data);
        
        if (!data.update_in_progress) {
            clearInterval(pollInterval);
            console.log('✅ Background analysis completed');
            
            // Show completion notification
            showCompletionNotification();
        }
    }, 5000); // Poll every 5 seconds
}

// Show a brief completion notification
function showCompletionNotification() {
    const notification = document.createElement('div');
    notification.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        background: #28a745;
        color: white;
        padding: 15px 20px;
        border-radius: 8px;
        z-index: 1000;
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        font-family: 'Poppins', Arial, sans-serif;
    `;
    notification.innerHTML = '<i class="fas fa-check-circle"></i> Chart analysis completed!';
    
    document.body.appendChild(notification);
    
    // Remove after 3 seconds
    setTimeout(() => {
        notification.style.transition = 'opacity 0.5s';
        notification.style.opacity = '0';
        setTimeout(() => document.body.removeChild(notification), 500);
    }, 3000);
}

// Summary cards double as the tier filter. Delegated from document because the
// cards are replaced wholesale on every render.
function handleSummaryCardActivation(event) {
    const card = event.target.closest('.summary-card.clickable');
    if (!card) return;

    if (event.type === 'keydown' && event.key !== 'Enter' && event.key !== ' ') {
        return;
    }
    event.preventDefault();
    onTierChange(card.dataset.tier);
}

document.addEventListener('click', handleSummaryCardActivation);
document.addEventListener('keydown', handleSummaryCardActivation);

// ---------------------------------------------------------------------------
// Diff viewer
// ---------------------------------------------------------------------------

// Bumped per open, so a slow response for one chart can't paint over another's
let diffRequestToken = 0;
let diffState = { chart: null, rawDiff: '', filename: 'helm.diff' };

// +++/--- are file headers and must be tested before the +/- content checks,
// or both header lines render as additions and deletions.
function classifyDiffLine(line) {
    if (line.startsWith('+++') || line.startsWith('---')) return 'meta';
    if (line.startsWith('@@')) return 'hunk';
    if (line.startsWith('+')) return 'add';
    if (line.startsWith('-')) return 'del';
    return 'ctx';
}

function diffElements() {
    return {
        modal: document.getElementById('diffModal'),
        title: document.getElementById('diffModalTitle'),
        meta: document.getElementById('diffMeta'),
        status: document.getElementById('diffStatus'),
        output: document.getElementById('diffOutput'),
        picker: document.getElementById('diffVersionPicker'),
        actions: document.getElementById('diffActions')
    };
}

function setDiffStatus(html) {
    diffElements().status.innerHTML = html;
}

// Rendered Kubernetes YAML always contains < and & (annotations, container args,
// embedded config), so diff text never goes through innerHTML. Building nodes and
// setting textContent makes escaping bugs structurally impossible rather than
// something to remember.
function renderDiffLines(text, limit) {
    const output = diffElements().output;
    output.textContent = '';

    const lines = text.split('\n');
    const shown = limit && lines.length > limit ? lines.slice(0, limit) : lines;
    const fragment = document.createDocumentFragment();

    shown.forEach(line => {
        const span = document.createElement('span');
        span.className = 'diff-line ' + classifyDiffLine(line);
        span.textContent = line;
        fragment.appendChild(span);
    });

    output.appendChild(fragment);

    if (shown.length < lines.length) {
        const more = document.createElement('button');
        more.className = 'diff-more-btn';
        more.type = 'button';
        more.textContent = `Show all ${lines.length} lines`;
        more.onclick = () => renderDiffLines(text, 0);
        output.appendChild(more);
    }
}

function renderDiff(result) {
    const { meta, output } = diffElements();
    diffState.rawDiff = result.diff || '';
    diffState.filename = `${result.chart_name}-${result.old_version}-to-${result.new_version}.diff`;

    meta.innerHTML = `
        <span class="diff-stat add">+${result.added}</span>
        <span class="diff-stat del">-${result.removed}</span>
        <span class="diff-stat">${result.line_count} lines</span>
        <span class="diff-stat">release <code>${esc(result.release_name)}</code></span>
        <span class="diff-stat">${result.cached ? 'cached' : result.elapsed_ms + 'ms'}</span>
    `;

    diffElements().actions.style.display = result.identical ? 'none' : '';

    if (result.identical) {
        output.textContent = '';
        setDiffStatus(`
            <div class="diff-note ok">
                <strong><i class="fas fa-check-circle"></i> No differences with default values.</strong>
                <p>Both versions render identically from the chart's defaults. Differences may
                   still exist in values you set in the cluster, or the bump may only change an
                   image tag that's referenced through values.</p>
            </div>
        `);
        return;
    }

    let note = `
        <div class="diff-note">
            Rendered from chart defaults, no cluster values - the same basis as the CI workflow.
            Charts that generate secrets at render time (randAlphaNum, genCA, uuidv4) produce
            spurious Secret and certificate changes.
        </div>
    `;
    if (result.truncated) {
        note += `<div class="diff-note warn"><strong>Diff truncated.</strong>
                 Download the full diff for everything.</div>`;
    }
    setDiffStatus(note);

    renderDiffLines(diffState.rawDiff, 2000);
}

function renderDiffError(error) {
    const { output, meta, actions } = diffElements();
    output.textContent = '';
    meta.innerHTML = '';
    actions.style.display = 'none';

    const detail = error && error.detail
        ? `<details class="diff-details"><summary>helm output</summary><pre></pre></details>`
        : '';

    setDiffStatus(`
        <div class="diff-note error">
            <strong><i class="fas fa-exclamation-triangle"></i>
                ${esc((error && error.code) || 'ERROR')}</strong>
            <p>${esc((error && error.message) || 'The diff could not be rendered.')}</p>
            ${detail}
        </div>
    `);

    // helm's stderr is untrusted text too - set it, don't interpolate it
    if (error && error.detail) {
        diffElements().status.querySelector('.diff-details pre').textContent = error.detail;
    }
}

async function runDiff(chart, oldVersion, newVersion, token) {
    const { output, meta, actions } = diffElements();
    meta.innerHTML = '';
    output.textContent = '';
    actions.style.display = 'none';
    setDiffStatus(`<div class="diff-loading"><span class="spinner"></span>
        Rendering ${esc(chart.chart_name)} ${esc(oldVersion)} &rarr; ${esc(newVersion)}...
        this usually takes a few seconds.</div>`);

    try {
        const response = await fetch('/api/diff', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                chart_name: chart.chart_name,
                repo_url: chart.repo_url,
                old_version: oldVersion,
                new_version: newVersion
            }),
            signal: AbortSignal.timeout(120000)
        });

        if (token !== diffRequestToken) return;   // a newer diff superseded this one

        const body = await response.json();
        if (!response.ok) {
            renderDiffError(body.error);
            return;
        }
        renderDiff(body);
    } catch (error) {
        if (token !== diffRequestToken) return;
        renderDiffError({
            code: error.name === 'TimeoutError' ? 'TIMEOUT' : 'NETWORK',
            message: error.name === 'TimeoutError'
                ? 'The diff took longer than two minutes and was cancelled.'
                : 'Could not reach the dashboard API.'
        });
    }
}

// Lets a reviewer step one release at a time instead of taking the whole jump
async function loadVersionOptions(chart, token) {
    const picker = diffElements().picker;
    picker.innerHTML = `
        <label>From</label>
        <select id="diffOldVersion"><option>${esc(chart.current_version)}</option></select>
        <label>to</label>
        <select id="diffNewVersion"><option>${esc(chart.latest_version)}</option></select>
    `;

    try {
        const response = await fetch('/api/versions?' + new URLSearchParams({
            chart: chart.chart_name, repo: chart.repo_url
        }));
        if (!response.ok || token !== diffRequestToken) return;

        const body = await response.json();
        const versions = body.versions || [];
        if (versions.length < 2) return;

        const options = selected => versions.map(v =>
            `<option value="${esc(v)}"${v === selected ? ' selected' : ''}>${esc(v)}</option>`
        ).join('');

        picker.innerHTML = `
            <label for="diffOldVersion">From</label>
            <select id="diffOldVersion">${options(chart.current_version)}</select>
            <label for="diffNewVersion">to</label>
            <select id="diffNewVersion">${options(chart.latest_version)}</select>
        `;

        const rerun = () => {
            const next = ++diffRequestToken;
            runDiff(chart,
                document.getElementById('diffOldVersion').value,
                document.getElementById('diffNewVersion').value,
                next);
        };
        document.getElementById('diffOldVersion').onchange = rerun;
        document.getElementById('diffNewVersion').onchange = rerun;
    } catch (error) {
        // Picker is a convenience; the default diff is already running
        console.warn('Could not load version list:', error);
    }
}

function openDiffModal(chart) {
    if (!chart) return;

    const token = ++diffRequestToken;
    diffState = { chart: chart, rawDiff: '', filename: 'helm.diff' };

    diffElements().title.textContent =
        `${chart.name}: ${chart.current_version} \u2192 ${chart.latest_version}`;

    bootstrap.Modal.getOrCreateInstance(diffElements().modal).show();

    loadVersionOptions(chart, token);
    runDiff(chart, chart.current_version, chart.latest_version, token);
}

async function copyDiff() {
    if (!diffState.rawDiff) return;
    const button = document.getElementById('diffCopyBtn');
    const original = button.innerHTML;

    try {
        // navigator.clipboard is unavailable on plain-HTTP origins, which an
        // internal dashboard may well be served over.
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(diffState.rawDiff);
        } else {
            const scratch = document.createElement('textarea');
            scratch.value = diffState.rawDiff;
            scratch.style.position = 'fixed';
            scratch.style.opacity = '0';
            document.body.appendChild(scratch);
            scratch.select();
            document.execCommand('copy');
            document.body.removeChild(scratch);
        }
        button.innerHTML = '<i class="fas fa-check"></i> Copied';
    } catch (error) {
        button.innerHTML = '<i class="fas fa-times"></i> Copy failed';
    }

    setTimeout(() => { button.innerHTML = original; }, 2000);
}

function downloadDiff() {
    if (!diffState.rawDiff) return;

    const blob = new Blob([diffState.rawDiff], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = diffState.filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
}

// Delegated: cards are replaced wholesale on every poll, so per-button handlers
// would not survive a refresh.
document.addEventListener('click', event => {
    const button = event.target.closest('.diff-btn');
    if (button) {
        openDiffModal(chartIndex.get(button.dataset.chartId));
    }
});

async function checkHelmAvailable() {
    try {
        const response = await fetch('/health');
        const body = await response.json();
        // Older builds don't report it; assume available rather than hiding the feature
        helmAvailable = body.helm_available !== false;
    } catch (error) {
        helmAvailable = true;
    }
}

// Load data when page loads
document.addEventListener('DOMContentLoaded', initializeData);