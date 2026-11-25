"""Fill unambiguous contact fields locally; no model or screenshots involved."""

import json
from typing import Any

from .profile import field_values

# Fixed application-owned script. Candidate strings are passed as JSON data.
_FILL_SCRIPT = r"""(values => {
    const normalize = value => String(value || '').toLowerCase()
        .replace(/[_-]+/g, ' ').replace(/[^a-z0-9 ]/g, '').trim()
        .replace(/\s+/g, ' ');
    const aliases = {
        'given-name': 'first name', 'family-name': 'last name',
        'name': 'full name', 'email': 'email', 'tel': 'phone',
        'address-level2': 'city', 'address-level1': 'state',
        'country-name': 'country', 'postal-code': 'postal'
    };
    const labels = {
        'first name': 'first name', 'last name': 'last name',
        'full name': 'full name', 'name': 'full name',
        'email': 'email', 'email address': 'email',
        'phone': 'phone', 'phone number': 'phone',
        'city': 'city', 'state': 'state', 'country': 'country',
        'zip': 'zip', 'zip code': 'zip', 'postal code': 'postal',
        'linkedin': 'linkedin', 'linkedin url': 'linkedin',
        'github': 'github', 'github url': 'github',
        'portfolio': 'portfolio', 'portfolio url': 'portfolio'
    };
    let filled = 0;
    for (const input of document.querySelectorAll('input')) {
        if (!['text', 'email', 'tel', 'url'].includes(input.type) ||
            input.disabled || input.readOnly || input.value.trim() ||
            !input.getClientRects().length ||
            getComputedStyle(input).visibility !== 'visible') continue;
        const names = [
            ...Array.from(input.labels || [], label => label.textContent),
            input.getAttribute('aria-label')
        ];
        const labelledBy = input.getAttribute('aria-labelledby') || '';
        if (labelledBy) names.push(labelledBy.split(/\s+/).map(
            id => document.getElementById(id)?.textContent || ''
        ).join(' '));
        const matches = new Set(names.map(name => labels[normalize(name)]).filter(Boolean));
        const auto = aliases[(input.autocomplete || '').split(/\s+/).pop()];
        if (auto) matches.add(auto);
        if (matches.size !== 1) continue;
        const value = values[[...matches][0]];
        if (!value) continue;
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        setter.call(input, value);
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.dispatchEvent(new Event('change', {bubbles: true}));
        filled++;
    }
    return {filled};
})"""


async def fill_known_fields(browser: Any, profile: dict[str, Any]) -> int:
    """Only fill visible, blank text inputs with exact labels/autocomplete matches.

    Custom controls, frames, shadow roots and ambiguous labels remain for the agent.
    """
    session = await browser.get_or_create_cdp_session()
    result = await session.cdp_client.send.Runtime.evaluate(
        params={
            "expression": f"{_FILL_SCRIPT}({json.dumps(field_values(profile))})",
            "returnByValue": True,
        },
        session_id=session.session_id,
    )
    if result.get("exceptionDetails"):
        raise RuntimeError("The local form-fill script failed.")
    return int(result.get("result", {}).get("value", {}).get("filled", 0))
