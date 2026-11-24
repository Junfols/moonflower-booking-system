/* Shared App QR Code Modal across Customer, Cashier, and Owner/Admin Dashboards */
(function () {
    'use strict';

    function getShareAppUrl() {
        try {
            return window.location.origin + '/';
        } catch (err) {
            return 'https://moonflower-booking-system.vercel.app/';
        }
    }

    function escapeAttr(str) {
        return String(str || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function generateQrMatrix(text) {
        if (typeof window.qrcode !== 'function') {
            throw new Error('QR library not loaded');
        }
        var qr = window.qrcode(0, 'M');
        qr.addData(text, 'Byte');
        qr.make();
        return qr;
    }

    function buildQrSvgMarkup(url, sizePx) {
        var qr = generateQrMatrix(url);
        var count = qr.getModuleCount();
        var margin = 4;
        var total = count + margin * 2;
        var size = sizePx || 228;
        var rects = '';

        for (var r = 0; r < count; r++) {
            for (var c = 0; c < count; c++) {
                if (qr.isDark(r, c)) {
                    rects += '<rect x="' + (c + margin) + '" y="' + (r + margin) + '" width="1" height="1"/>';
                }
            }
        }

        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + total + ' ' + total + '" ' +
            'width="' + size + '" height="' + size + '" shape-rendering="crispEdges" ' +
            'role="img" aria-label="QR code for ' + escapeAttr(url) + '" ' +
            'style="display:block; margin:0 auto; border-radius:12px; background:#ffffff;">' +
            '<rect width="' + total + '" height="' + total + '" fill="#ffffff"/>' +
            '<g fill="#10372A">' + rects + '</g>' +
            '</svg>'
        );
    }

    function notifyUser(msg, isError) {
        if (typeof window.showToast === 'function') {
            window.showToast(msg, Boolean(isError));
        }
    }

    function copyAppShareUrl() {
        var url = getShareAppUrl();
        var btn = document.getElementById('appQrCopyBtn');
        var markCopied = function () {
            if (btn) {
                var span = btn.querySelector('span');
                if (span) {
                    var prev = span.textContent;
                    span.textContent = 'Copied!';
                    setTimeout(function () { span.textContent = prev; }, 1800);
                }
            }
            notifyUser('App link copied: ' + url, false);
        };
        var fallbackCopy = function () {
            try {
                var ta = document.createElement('textarea');
                ta.value = url;
                ta.setAttribute('readonly', '');
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                markCopied();
            } catch (err) {
                notifyUser(url, false);
            }
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(url).then(markCopied).catch(fallbackCopy);
        } else {
            fallbackCopy();
        }
    }

    function downloadAppQrPng() {
        var url = getShareAppUrl();
        try {
            var qr = generateQrMatrix(url);
            var count = qr.getModuleCount();
            var canvas = document.createElement('canvas');
            var w = 640;
            var h = 760;
            canvas.width = w;
            canvas.height = h;
            var ctx = canvas.getContext('2d');

            // Warm ivory poster backdrop
            ctx.fillStyle = '#F6F5F0';
            ctx.fillRect(0, 0, w, h);

            // Top dark emerald band
            ctx.fillStyle = '#10372A';
            ctx.fillRect(0, 0, w, 146);

            ctx.fillStyle = '#F0D29A';
            ctx.font = 'bold 20px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('CAMIGUIN • SINCE 2011', w / 2, 50);

            ctx.fillStyle = '#FFFFFF';
            ctx.font = 'bold 38px Georgia, serif';
            ctx.fillText('MoonFlower Garden', w / 2, 96);

            ctx.fillStyle = 'rgba(255,255,255,0.85)';
            ctx.font = '18px sans-serif';
            ctx.fillText('Resort Rooms & Fine Dining App', w / 2, 126);

            // Crisp pure-white quiet-zone card around QR
            var qrCardSize = 440;
            var cardX = (w - qrCardSize) / 2;
            var cardY = 180;
            ctx.fillStyle = '#FFFFFF';
            ctx.fillRect(cardX, cardY, qrCardSize, qrCardSize);
            ctx.strokeStyle = 'rgba(16, 55, 42, 0.14)';
            ctx.lineWidth = 2;
            ctx.strokeRect(cardX, cardY, qrCardSize, qrCardSize);

            var margin = 4;
            var total = count + margin * 2;
            var modPx = Math.floor(qrCardSize / total);
            var qrDrawSize = modPx * count;
            var startX = Math.round((w - qrDrawSize) / 2);
            var startY = Math.round(cardY + (qrCardSize - qrDrawSize) / 2);

            ctx.fillStyle = '#10372A';
            for (var r = 0; r < count; r++) {
                for (var c = 0; c < count; c++) {
                    if (qr.isDark(r, c)) {
                        ctx.fillRect(startX + c * modPx, startY + r * modPx, modPx, modPx);
                    }
                }
            }

            ctx.fillStyle = '#141F1B';
            ctx.font = 'bold 24px sans-serif';
            ctx.fillText('Scan with Phone Camera to Open App', w / 2, 668);

            ctx.fillStyle = '#516059';
            ctx.font = '18px monospace';
            var displayUrl = url.replace(/\/$/, '');
            ctx.fillText(displayUrl, w / 2, 706);

            var link = document.createElement('a');
            link.download = 'moonflower-garden-app-qr.png';
            link.href = canvas.toDataURL('image/png');
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            notifyUser('QR Code image saved!', false);
        } catch (err) {
            notifyUser('Unable to export QR PNG on this browser.', true);
        }
    }

    function nativeShareAppQr() {
        var url = getShareAppUrl();
        if (navigator.share) {
            navigator.share({
                title: 'MoonFlower Garden Camiguin App',
                text: 'Book rooms, browse the food menu, and reserve your table at MoonFlower Garden:',
                url: url
            }).catch(function () {});
        } else {
            copyAppShareUrl();
        }
    }

    function ensureAppQrModal() {
        var existing = document.getElementById('appQrShareModal');
        if (existing) return existing;

        var modal = document.createElement('div');
        modal.id = 'appQrShareModal';
        modal.className = 'modal-backdrop hidden';
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');
        modal.setAttribute('aria-labelledby', 'appQrModalTitle');

        var canShare = (typeof navigator.share === 'function');
        modal.innerHTML =
            '<div class="modal-card" style="max-width: 400px; text-align: center;">' +
                '<button type="button" id="appQrCloseBtn" class="modal-close" aria-label="Close QR Code modal">' +
                    '<i class="fa-solid fa-xmark"></i>' +
                '</button>' +
                '<div style="display:flex; align-items:center; justify-content:center; gap:0.55rem; margin-bottom:0.35rem;">' +
                    '<img src="/static/icons/icon-192.png" alt="MoonFlower Garden" style="width:32px; height:32px; border-radius:9px; object-fit:cover;">' +
                    '<span class="brand-subtitle" style="margin:0;">MoonFlower Garden</span>' +
                '</div>' +
                '<h3 id="appQrModalTitle" class="font-display" style="font-size: 1.55rem; font-weight: 700; color: var(--brand-primary); margin-bottom: 0.25rem;">Share MoonFlower App</h3>' +
                '<p style="font-size: 0.82rem; color: var(--text-secondary); margin-bottom: 1rem;">Have someone scan this QR code with their camera to open the booking app right away.</p>' +
                '<div id="appQrSvgContainer" style="display:inline-flex; padding:0.7rem; background:#ffffff; border:1px solid var(--border-subtle); border-radius:18px; box-shadow: var(--shadow-sm); margin-bottom: 0.85rem;"></div>' +
                '<div style="background: var(--bg-elevated); border: 1px solid var(--border-subtle); border-radius: 10px; padding: 0.48rem 0.72rem; margin-bottom: 1rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">' +
                    '<span id="appQrUrlText" class="font-mono" style="font-size: 0.74rem; color: var(--brand-primary); font-weight: 600;"></span>' +
                '</div>' +
                '<div style="display: flex; gap: 0.5rem; flex-wrap: wrap;">' +
                    '<button type="button" id="appQrCopyBtn" class="btn btn-outline btn-sm" style="flex: 1; min-width: 105px;">' +
                        '<i class="fa-regular fa-copy"></i>' +
                        '<span>Copy Link</span>' +
                    '</button>' +
                    '<button type="button" id="appQrSaveBtn" class="btn btn-primary btn-sm" style="flex: 1; min-width: 105px;">' +
                        '<i class="fa-solid fa-download"></i>' +
                        '<span>Save QR</span>' +
                    '</button>' +
                    (canShare
                        ? '<button type="button" id="appQrNativeShareBtn" class="btn btn-accent btn-sm" style="flex: 1 1 100%;">' +
                              '<i class="fa-solid fa-share-nodes"></i>' +
                              '<span>Share Link to App / Chat</span>' +
                          '</button>'
                        : '') +
                '</div>' +
            '</div>';

        document.body.appendChild(modal);

        document.getElementById('appQrCloseBtn').addEventListener('click', closeAppQrModal);
        document.getElementById('appQrCopyBtn').addEventListener('click', copyAppShareUrl);
        document.getElementById('appQrSaveBtn').addEventListener('click', downloadAppQrPng);
        var shareBtn = document.getElementById('appQrNativeShareBtn');
        if (shareBtn) {
            shareBtn.addEventListener('click', nativeShareAppQr);
        }
        modal.addEventListener('click', function (evt) {
            if (evt.target === modal) closeAppQrModal();
        });
        document.addEventListener('keydown', function (evt) {
            if (evt.key === 'Escape' && !modal.classList.contains('hidden')) {
                closeAppQrModal();
            }
        });

        return modal;
    }

    function openAppQrModal() {
        var modal = ensureAppQrModal();
        var url = getShareAppUrl();
        var svgBox = document.getElementById('appQrSvgContainer');
        var urlEl = document.getElementById('appQrUrlText');
        if (urlEl) {
            urlEl.textContent = url.replace(/\/$/, '');
        }
        if (svgBox) {
            try {
                svgBox.innerHTML = buildQrSvgMarkup(url, 224);
            } catch (err) {
                svgBox.innerHTML = '<div style="padding:1rem; font-size:0.82rem; color:var(--text-secondary);">Unable to generate QR right now. Copy link below instead.</div>';
            }
        }
        modal.classList.remove('hidden');
    }

    function closeAppQrModal() {
        var modal = document.getElementById('appQrShareModal');
        if (modal) {
            modal.classList.add('hidden');
        }
    }

    window.openAppQrModal = openAppQrModal;
    window.closeAppQrModal = closeAppQrModal;
    window.buildMoonflowerQrSvg = buildQrSvgMarkup;

    if (typeof window.location !== 'undefined' && window.location.search.indexOf('qr=1') !== -1) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', openAppQrModal);
        } else {
            openAppQrModal();
        }
    }
})();
