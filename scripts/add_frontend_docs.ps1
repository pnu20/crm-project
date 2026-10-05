$path = Join-Path $PSScriptRoot '..\frontend\app.js'
$source = Get-Content -Raw -Encoding UTF8 $path
$docs = [ordered]@{
    translate = 'Translate an English label with an English fallback.'
    translateText = 'Translate a text node without altering customer-provided content.'
    translatePage = 'Apply language, direction, and localized counts across the page.'
    toast = 'Show a brief status message to the user.'
    api = 'Send a request to FastAPI and return the response data.'
    refreshData = 'Load contacts, deals, and tasks from the API and rerender.'
    update = 'Redraw the CRM after presentation state changes.'
    render = 'Rebuild all dashboard and list views from current records.'
    renderCounts = 'Update summary counters from the loaded CRM records.'
    renderDashboard = 'Render pipeline totals, recent activity, and daily tasks.'
    renderContacts = 'Filter and render contacts in the active language.'
    renderDeals = 'Render deals grouped by their pipeline stage.'
    renderTasks = 'Filter and render tasks by completion state.'
    showView = 'Switch the visible section and update translated navigation.'
    newContact = 'Prepare and open the new-contact form.'
    detailContact = 'Build and open the selected contact profile.'
    editContact = 'Populate the form with an existing contact.'
    reportApiError = 'Log an API failure and show a localized save error.'
}
foreach ($name in $docs.Keys) {
    $pattern = [regex]::new("(?m)^([ \t]*)(?:async[ \t]+)?function[ \t]+$([regex]::Escape($name))\(")
    if (-not $pattern.IsMatch($source)) { throw "Could not find function $name" }
    $description = $docs[$name]
    $source = $pattern.Replace($source, {
        param($match)
        $match.Groups[1].Value + '/** ' + $description + ' */' + [Environment]::NewLine + $match.Value
    }, 1)
}
Set-Content -Path $path -Value $source -Encoding UTF8
