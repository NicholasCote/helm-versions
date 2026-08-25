// Helm Chart Dashboard JavaScript
let refreshInterval;

// Cluster filter state. 'all' shows every cluster the API returned.
const CLUSTER_FILTER_KEY = 'helmDashboard.selectedCluster';
let selectedCluster = loadSelectedCluster();
let latestData = null;  // Cached so switching clusters re-renders without a refetch

function loadSelectedCluster() {
    try {
        return localStorage.getItem(CLUSTER_FILTER_KEY) || 'all';
    } catch (e) {
        return 'all';
    }
}

function saveSelectedCluster(value) {
    try {
        localStorage.setItem(CLUSTER_FILTER_KEY, value);
    } catch (e) {
        // Storage unavailable (private mode, blocked cookies) - filter still works
    }
}

function onClusterChange(value) {
    selectedCluster = value;
    saveSelectedCluster(value);
    if (latestData) {
        renderData(latestData);
    }
}

// Rebuild the dropdown options to match the clusters actually present in the data
function populateClusterFilter(clusters) {
    const select = document.getElementById('clusterFilter');
    if (!select) return;

    const names = Object.keys(clusters);

    // A remembered cluster may not exist anymore (e.g. CLUSTERS was narrowed)
    if (selectedCluster !== 'all' && !names.includes(selectedCluster)) {
        selectedCluster = 'all';
        saveSelectedCluster(selectedCluster);
    }

    const wanted = ['all'].concat(names).join(',');
    if (select.dataset.options !== wanted) {
        select.innerHTML = '<option value="all">All clusters</option>' +
            names.map(name => `<option value="${name}">${name.toUpperCase()}</option>`).join('');
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

// Totals for whatever is currently visible, so the cards match the dropdown
function summarizeClusters(clusters) {
    return Object.values(clusters).reduce((totals, cluster) => {
        const summary = cluster.summary || {};
        totals.total_charts += summary.total_charts || 0;
        totals.needs_update += summary.needs_update || 0;
        totals.up_to_date += summary.up_to_date || 0;
        totals.no_version_info += summary.no_version_info || 0;
        return totals;
    }, { total_charts: 0, needs_update: 0, up_to_date: 0, no_version_info: 0 });
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

function updateSummaryCards(summary) {
    if (!summary) return;
    
    document.getElementById('summaryCards').innerHTML = `
        <div class="summary-card total">
            <div class="card-header">Total Charts</div>
            <div class="card-body">
                <div class="number">${summary.total_charts}</div>
            </div>
        </div>
        <div class="summary-card updates">
            <div class="card-header">Need Updates</div>
            <div class="card-body">
                <div class="number">${summary.needs_update}</div>
            </div>
        </div>
        <div class="summary-card current">
            <div class="card-header">Up to Date</div>
            <div class="card-body">
                <div class="number">${summary.up_to_date}</div>
            </div>
        </div>
        <div class="summary-card unknown">
            <div class="card-header">Unknown Status</div>
            <div class="card-body">
                <div class="number">${summary.no_version_info}</div>
            </div>
        </div>
    `;
}

function renderChartCard(chart) {
    let status = 'unknown';
    let statusClass = 'unknown';
    
    if (chart.latest_version) {
        if (chart.needs_update) {
            status = 'needs-update';
            statusClass = 'needs-update';
        } else {
            status = 'up-to-date';
            statusClass = 'up-to-date';
        }
    }

    const versionDisplay = chart.latest_version ? `
        <div class="chart-versions">
            <span class="version current">${chart.current_version}</span>
            ${chart.needs_update ? '<span class="arrow"><i class="fas fa-arrow-right"></i></span>' : ''}
            <span class="version latest">${chart.latest_version}</span>
        </div>
    ` : `
        <div class="chart-versions">
            <span class="version current">${chart.current_version}</span>
            <span style="color: #999; font-style: italic;">${chart.no_version_reason || 'No version info'}</span>
        </div>
    `;

    return `
        <div class="chart-card ${statusClass}">
            <div class="chart-name"><i class="fas fa-cube"></i> ${chart.name}${chart.source ? `<span class="chart-source">${chart.source}</span>` : ''}</div>
            ${versionDisplay}
            ${chart.repo_url ? `<div class="chart-repo"><i class="fas fa-link"></i> ${chart.repo_url}</div>` : ''}
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
            updateSummaryCards(summarizeClusters(visible));
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
                <p>${data.error}</p>
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
    updateSummaryCards(summarizeClusters(visible));
    document.getElementById('content').innerHTML = generateClustersHtml(visible);
    document.getElementById('lastUpdate').textContent = formatDate(data.last_update);
}

function generateClustersHtml(clusters) {
    let html = '';

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
        const summary = cluster.summary;

        html += `
            <div class="cluster-section">
                <div class="cluster-header">
                    <div class="cluster-name"><i class="fas fa-server"></i> ${clusterName.toUpperCase()}</div>
                    <div class="cluster-stats">
                        ${summary.needs_update > 0 ? `<span class="stat updates">${summary.needs_update} updates</span>` : ''}
                        ${summary.up_to_date > 0 ? `<span class="stat current">${summary.up_to_date} current</span>` : ''}
                        ${summary.no_version_info > 0 ? `<span class="stat unknown">${summary.no_version_info} unknown</span>` : ''}
                    </div>
                </div>
                <div class="charts-container">
                    <div class="charts-grid">
                        ${cluster.charts.map(chart => renderChartCard(chart)).join('')}
                    </div>
                </div>
            </div>
        `;
    });
    
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

// Load data when page loads
document.addEventListener('DOMContentLoaded', initializeData);