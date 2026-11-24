/* Shared Theme Switcher across Customer, Cashier, and Owner/Admin Dashboards */
(function () {
    'use strict';

    var STORAGE_KEY = 'mf_theme_override_v1';

    var THEMES = [
        {
            id: 'emerald',
            name: 'Botanical Emerald',
            desc: 'Classic Resort & Garden',
            bg: '#F6F5F0',
            surface: '#FFFFFF',
            primary: '#10372A',
            accent: '#B87626',
            isDark: false
        },
        {
            id: 'lagoon',
            name: 'Seafront Lagoon',
            desc: 'Camiguin Ocean Teal',
            bg: '#F2F7F8',
            surface: '#FFFFFF',
            primary: '#0F4C5C',
            accent: '#C97A40',
            isDark: false
        },
        {
            id: 'sunset',
            name: 'Tropical Sunset',
            desc: 'Sunset Terracotta & Mango',
            bg: '#FCF7F1',
            surface: '#FFFFFF',
            primary: '#8A3319',
            accent: '#D97706',
            isDark: false
        },
        {
            id: 'midnight',
            name: 'Midnight Bar Lounge',
            desc: 'Dark Mode & Gold',
            bg: '#101514',
            surface: '#192220',
            primary: '#E2B76A',
            accent: '#34D399',
            isDark: true
        },
        {
            id: 'kyoto',
            name: 'Kyoto Fusion Bistro',
            desc: 'Washi Charcoal & Crimson',
            bg: '#F8F6F1',
            surface: '#FFFFFF',
            primary: '#23211E',
            accent: '#B91C1C',
            isDark: false
        },
        {
            id: 'gelato',
            name: 'Matcha & Berry',
            desc: 'Garden Cafe & Desserts',
            bg: '#FBF9F5',
            surface: '#FFFFFF',
            primary: '#2D5A43',
            accent: '#C04862',
            isDark: false
        }
    ];

    function findTheme(id) {
        for (var i = 0; i < THEMES.length; i++) {
            if (THEMES[i].id === id) return THEMES[i];
        }
        return THEMES[0];
    }

    function getDefaultTheme() {
        var raw = document.documentElement.getAttribute('data-default-theme') || 'emerald';
        return findTheme(raw).id;
    }

    function getActiveTheme() {
        var cur = document.documentElement.getAttribute('data-theme');
        if (cur) return findTheme(cur).id;
        return getDefaultTheme();
    }

    function updateNavIndicator() {
        var active = findTheme(getActiveTheme());
        var dot = document.getElementById('themeSwatchDot');
        if (dot) {
            dot.style.background = active.primary;
        }
        var meta = document.querySelector('meta[name="theme-color"]');
        if (meta) {
            meta.setAttribute('content', active.isDark ? '#101514' : active.primary);
        }
    }

    function applyMoonflowerTheme(themeId, persist) {
        var t = findTheme(themeId);
        document.documentElement.setAttribute('data-theme', t.id);
        if (persist !== false) {
            try {
                localStorage.setItem(STORAGE_KEY, t.id);
            } catch (err) {}
        }
        updateNavIndicator();
        renderThemeModalOptions();
    }

    function resetToDefaultTheme() {
        var defId = getDefaultTheme();
        try {
            localStorage.removeItem(STORAGE_KEY);
        } catch (err) {}
        applyMoonflowerTheme(defId, false);
        if (typeof window.showToast === 'function') {
            window.showToast('Restored Resort Default Theme: ' + findTheme(defId).name);
        }
    }

    function renderThemeModalOptions() {
        var grid = document.getElementById('appThemeGrid');
        if (!grid) return;

        var activeId = getActiveTheme();
        var defaultId = getDefaultTheme();
        var hasOverride = false;
        try {
            hasOverride = Boolean(localStorage.getItem(STORAGE_KEY));
        } catch (err) {}

        var resetBtn = document.getElementById('appThemeResetBtn');
        if (resetBtn) {
            resetBtn.style.display = (hasOverride && activeId !== defaultId) ? 'inline-flex' : 'none';
        }

        grid.innerHTML = '';
        THEMES.forEach(function (t) {
            var isSelected = (t.id === activeId);
            var isDefault = (t.id === defaultId);

            var card = document.createElement('button');
            card.type = 'button';
            card.className = isSelected ? 'theme-option-card is-selected' : 'theme-option-card';
            card.onclick = function () {
                applyMoonflowerTheme(t.id, true);
                if (typeof window.showToast === 'function') {
                    window.showToast('Theme switched: ' + t.name);
                }
            };

            card.innerHTML =
                '<div class="theme-swatch-strip">' +
                    '<span class="theme-swatch-chip" style="background:' + t.bg + ';"></span>' +
                    '<span class="theme-swatch-chip" style="background:' + t.primary + ';"></span>' +
                    '<span class="theme-swatch-chip" style="background:' + t.accent + ';"></span>' +
                '</div>' +
                '<div style="text-align:left; min-width:0; flex:1;">' +
                    '<div style="display:flex; align-items:center; justify-content:space-between; gap:0.35rem;">' +
                        '<strong style="font-size:0.84rem; color:var(--text-primary); line-height:1.2;">' + t.name + '</strong>' +
                        (isSelected ? '<i class="fa-solid fa-circle-check" style="color:var(--brand-primary); font-size:0.9rem;"></i>' : '') +
                    '</div>' +
                    '<div style="font-size:0.72rem; color:var(--text-secondary); margin-top:2px;">' + t.desc + '</div>' +
                    (isDefault ? '<span class="theme-default-tag">Resort Default</span>' : '') +
                '</div>';

            grid.appendChild(card);
        });
    }

    function ensureAppThemeModal() {
        var existing = document.getElementById('appThemeModal');
        if (existing) return existing;

        var modal = document.createElement('div');
        modal.id = 'appThemeModal';
        modal.className = 'modal-backdrop hidden';
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');
        modal.setAttribute('aria-labelledby', 'appThemeModalTitle');

        modal.innerHTML =
            '<div class="modal-card" style="max-width: 480px;">' +
                '<button type="button" id="appThemeCloseBtn" class="modal-close" aria-label="Close theme switcher">' +
                    '<i class="fa-solid fa-xmark"></i>' +
                '</button>' +
                '<div style="margin-bottom: 1rem;">' +
                    '<span class="brand-subtitle">Appearance &amp; Mood</span>' +
                    '<h3 id="appThemeModalTitle" class="font-display" style="font-size: 1.55rem; font-weight: 700;">Choose Theme</h3>' +
                    '<p style="font-size: 0.82rem; color: var(--text-secondary);">Switch color palettes anytime across rooms, dining menu, and portals.</p>' +
                '</div>' +
                '<div id="appThemeGrid" class="theme-picker-grid"></div>' +
                '<div style="display:flex; align-items:center; justify-content:space-between; gap:0.65rem; flex-wrap:wrap; margin-top: 1.1rem; padding-top: 0.85rem; border-top: 1px solid var(--border-subtle);">' +
                    '<button type="button" id="appThemeResetBtn" class="btn btn-outline btn-sm">' +
                        '<i class="fa-solid fa-rotate-left"></i>' +
                        '<span>Use Resort Default</span>' +
                    '</button>' +
                    '<button type="button" id="appThemeDoneBtn" class="btn btn-primary btn-sm" style="margin-left:auto;">Done</button>' +
                '</div>' +
            '</div>';

        document.body.appendChild(modal);

        document.getElementById('appThemeCloseBtn').addEventListener('click', closeThemeSwitcherModal);
        document.getElementById('appThemeDoneBtn').addEventListener('click', closeThemeSwitcherModal);
        document.getElementById('appThemeResetBtn').addEventListener('click', resetToDefaultTheme);
        modal.addEventListener('click', function (evt) {
            if (evt.target === modal) closeThemeSwitcherModal();
        });
        document.addEventListener('keydown', function (evt) {
            if (evt.key === 'Escape' && !modal.classList.contains('hidden')) {
                closeThemeSwitcherModal();
            }
        });

        return modal;
    }

    function openThemeSwitcherModal() {
        var modal = ensureAppThemeModal();
        renderThemeModalOptions();
        modal.classList.remove('hidden');
    }

    function closeThemeSwitcherModal() {
        var modal = document.getElementById('appThemeModal');
        if (modal) modal.classList.add('hidden');
    }

    window.openThemeSwitcherModal = openThemeSwitcherModal;
    window.closeThemeSwitcherModal = closeThemeSwitcherModal;
    window.applyMoonflowerTheme = applyMoonflowerTheme;
    window.resetToDefaultTheme = resetToDefaultTheme;
    window.MOONFLOWER_THEMES = THEMES;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', updateNavIndicator);
    } else {
        updateNavIndicator();
    }
})();
