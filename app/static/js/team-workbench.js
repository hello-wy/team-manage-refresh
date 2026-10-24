/* Team row menus live outside the animated table; seat invitations reuse member management. */
(() => {
    let activeTrigger = null;
    const menuNode = () => document.getElementById('teamRowMenu');

    function closeMenu(restoreFocus = false) {
        const menu = menuNode();
        if (!menu || menu.hidden) return;
        if (typeof menu.hidePopover === 'function' && menu.matches(':popover-open')) menu.hidePopover();
        menu.hidden = true;
        const trigger = activeTrigger;
        activeTrigger = null;
        trigger?.setAttribute('aria-expanded', 'false');
        if (restoreFocus && trigger?.isConnected) trigger.focus({preventScroll: true});
    }

    function menuItems(trigger) {
        const deviceAuthEnabled = trigger.dataset.deviceAuth === 'true';
        const items = [];
        items.push(['device-auth', 'badge-check', deviceAuthEnabled ? '设备验证已开启' : '开启设备验证', deviceAuthEnabled]);
        items.push(['push', 'send', '推送母号到 sub2api'], ['export', 'download', '导出 JSON'], null,
            ['delete', 'trash-2', '删除 Team', false, 'is-danger']);
        return `<div class="team-row-menu-header" role="presentation"><span class="team-row-menu-title">Team 操作</span>
            <span class="team-row-menu-email">${escapeHtml(trigger.dataset.email)}</span></div>`
            + items.map(item => item ? `<button type="button" role="menuitem" data-action="${item[0]}"
                class="${item[4] || ''}" ${item[3] ? 'disabled' : ''}><i data-lucide="${item[1]}" aria-hidden="true"></i>${item[2]}</button>`
                : '<hr role="separator">').join('');
    }

    function openMenu(trigger) {
        const menu = menuNode();
        if (!menu) return;
        if (activeTrigger === trigger && !menu.hidden) return closeMenu(true);
        closeMenu();
        closeFloatingDropdowns();
        // Animated ancestors establish a containing block for position: fixed.
        if (menu.parentElement !== document.body) document.body.appendChild(menu);
        menu.innerHTML = menuItems(trigger);
        menu.hidden = false;
        if (typeof menu.showPopover === 'function') {
            menu.setAttribute('popover', 'manual');
            menu.showPopover();
        }
        if (window.lucide) lucide.createIcons();
        const rect = trigger.getBoundingClientRect();
        const {offsetWidth: width, offsetHeight: height} = menu;
        const padding = 12;
        const below = rect.bottom + 8;
        const top = below + height <= window.innerHeight - padding
            ? below : Math.max(padding, rect.top - height - 8);
        menu.style.top = `${top}px`;
        menu.style.left = `${Math.max(padding, Math.min(rect.right - width, window.innerWidth - width - padding))}px`;
        activeTrigger = trigger;
        trigger.setAttribute('aria-expanded', 'true');
        menu.querySelector('button:not(:disabled)')?.focus({preventScroll: true});
    }

    function runAction(action) {
        const trigger = activeTrigger;
        closeMenu(true);
        if (!trigger) return;
        const {id, email, deviceAuth} = trigger.dataset;
        if (action === 'device-auth') enableDeviceAuth(id, deviceAuth === 'true');
        else if (action === 'push') pushTeamToSub2api(id, email);
        else if (action === 'export') exportTeamJson(id, email);
        else if (action === 'delete') deleteTeam(Number(id), email);
    }

    document.addEventListener('click', event => {
        const trigger = event.target.closest('#teamTableRegion .btn-team-more');
        if (trigger) return openMenu(trigger);
        const menu = menuNode();
        const item = event.target.closest('#teamRowMenu button[data-action]');
        if (item && !item.disabled) return runAction(item.dataset.action);
        if (!menu?.contains(event.target)) closeMenu();
    });

    document.addEventListener('keydown', event => {
        const menu = menuNode();
        if (!menu || menu.hidden) return;
        if (event.key === 'Escape' || event.key === 'Tab') return closeMenu(true);
        if (!menu.contains(event.target) || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const items = [...menu.querySelectorAll('button:not(:disabled)')];
        const index = items.indexOf(document.activeElement);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
            : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items[next]?.focus({preventScroll: true});
    });
    window.addEventListener('resize', () => closeMenu());
    window.addEventListener('scroll', event => {
        if (!menuNode()?.contains(event.target)) closeMenu();
    }, true);

    window.teamWorkbench = {closeMenu};
})();
