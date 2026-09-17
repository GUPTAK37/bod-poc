/*
 * Tableau-style multi-select summary.
 *
 * For every dropdown carrying the `summarized-dd` class we watch its
 * internal react-select DOM and replace the chip list with a plain-text
 * summary:
 *     0 selections  → the default placeholder (e.g. "(All)") shows
 *     1 selection   → the single chip / label shows as-is
 *     2+ selections → all chips are hidden and the text "(Multiple Values)"
 *                     is displayed instead
 *
 * The behaviour is idempotent + guarded so the MutationObserver does not
 * loop on its own DOM writes.
 */
(function () {
    let applying = false;
    let scheduled = false;

    function applyToDropdown(container) {
        const valueContainer = container.querySelector(
            '[class*="ValueContainer"], [class*="valueContainer"]'
        );
        if (!valueContainer) return;

        const chips = valueContainer.querySelectorAll('[class*="multiValue"]');
        let summary = valueContainer.querySelector(':scope > .dd-summary-text');

        if (chips.length >= 2) {
            // Hide chips, show "(Multiple Values)".
            chips.forEach((c) => {
                if (c.style.display !== 'none') c.style.display = 'none';
            });
            if (!summary) {
                summary = document.createElement('div');
                summary.className = 'dd-summary-text';
                summary.textContent = '(Multiple Values)';
                valueContainer.insertBefore(summary, valueContainer.firstChild);
            }
            if (summary.style.display === 'none') summary.style.display = '';
        } else {
            // Restore chips, hide summary.
            chips.forEach((c) => {
                if (c.style.display === 'none') c.style.display = '';
            });
            if (summary && summary.style.display !== 'none') {
                summary.style.display = 'none';
            }
        }
    }

    function applyAll() {
        if (applying) return;
        applying = true;
        try {
            document
                .querySelectorAll('.summarized-dd')
                .forEach(applyToDropdown);
        } finally {
            applying = false;
        }
    }

    function schedule() {
        if (scheduled) return;
        scheduled = true;
        requestAnimationFrame(() => {
            scheduled = false;
            applyAll();
        });
    }

    function boot() {
        applyAll();
        const observer = new MutationObserver(function () {
            schedule();
        });
        observer.observe(document.body, {
            childList: true,
            subtree: true,
            attributes: false,
        });
        // Belt-and-suspenders: a 300 ms interval catches any edge cases
        // where react-select re-renders without a top-level mutation.
        setInterval(applyAll, 300);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
