import React, { useState } from 'react';

/** Compact, optional manufacturer contact panel shown only for supported machines. */
export default function ManufacturerSupport({ support }) {
  const [open, setOpen] = useState(false);
  if (!support || !Array.isArray(support.contacts) || support.contacts.length === 0) return null;

  return (
    <section className="manufacturer-support" aria-label="Manufacturer support">
      <div className="manufacturer-support-heading">
        <div>
          <div className="manufacturer-support-kicker">Manufacturer support</div>
          <h3>{support.title || `Need more help from ${support.manufacturer || 'the manufacturer'}?`}</h3>
          {support.manufacturer && <p>{support.manufacturer}</p>}
        </div>
        <button type="button" onClick={() => setOpen(value => !value)} aria-expanded={open}>
          {open ? 'Hide contacts' : 'Show contacts'}
        </button>
      </div>
      {open && (
        <div className="manufacturer-support-details">
          <ul>
            {support.contacts.map((contact, index) => (
              <li key={`${contact.label || 'contact'}-${index}`}>
                <span>{contact.label}</span>
                <a href={contact.href}>{contact.value}</a>
                {contact.note && <p>{contact.note}</p>}
                {contact.source && <small>Source: {contact.source}</small>}
              </li>
            ))}
          </ul>
          {support.note && <p className="manufacturer-support-note">{support.note}</p>}
          {support.website?.href && (
            <a className="manufacturer-support-website" href={support.website.href} target="_blank" rel="noreferrer">
              {support.website.label || 'Open manufacturer website'}
            </a>
          )}
        </div>
      )}
    </section>
  );
}
