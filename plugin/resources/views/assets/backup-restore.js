/*!
 * LibreNMS Backup & Restore - GUI client
 *
 * Vanilla JS on purpose: LibreNMS ships Bootstrap 3 + jQuery, but a plugin
 * cannot rely on extra build steps. No external CDNs are used, so the UI works
 * on air-gapped monitoring servers.
 *
 * Security notes:
 *  - every mutation carries the CSRF token from the server-rendered payload
 *  - no secrets are ever placed in the DOM; the server sends status only
 *  - destructive actions require a typed confirmation phrase
 */
(function () {
    "use strict";

    var B = window.BR_BOOTSTRAP || {};
    var root = null;
    var state = { tab: "dashboard", busy: false, timer: null, jobId: null, lastLog: [] };

    // ---------------------------------------------------------------- utils
    function el(tag, attrs, children) {
        var node = document.createElement(tag);
        attrs = attrs || {};
        Object.keys(attrs).forEach(function (k) {
            if (k === "class") node.className = attrs[k];
            else if (k === "html") node.innerHTML = attrs[k];
            else if (k === "text") node.textContent = attrs[k];
            else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), attrs[k]);
            else if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
        });
        (children || []).forEach(function (c) {
            if (c === null || c === undefined) return;
            node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
        });
        return node;
    }

    function esc(value) {
        return String(value === null || value === undefined ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function bytes(n) {
        n = Number(n || 0);
        if (n < 1024) return n + " B";
        var u = ["KiB", "MiB", "GiB", "TiB"], i = -1;
        do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
        return n.toFixed(1) + " " + u[i];
    }

    function when(iso) {
        if (!iso) return "\u2014";
        var d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso);
        var diff = (Date.now() - d.getTime()) / 1000;
        if (diff >= 0 && diff < 90) return Math.round(diff) + "s ago";
        if (diff >= 0 && diff < 5400) return Math.round(diff / 60) + "m ago";
        return d.toLocaleString();
    }

    function api(path, options) {
        options = options || {};
        var url = B.endpoint + "?r=" + encodeURIComponent(path);
        var init = {
            method: options.method || "GET",
            credentials: "same-origin",
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-TOKEN": B.csrf || ""
            }
        };
        if (options.body) {
            var form = new URLSearchParams();
            Object.keys(options.body).forEach(function (k) {
                form.append(k, options.body[k]);
            });
            form.append("_token", B.csrf);
            init.body = form;
            init.headers["Content-Type"] = "application/x-www-form-urlencoded";
            if (init.method === "GET") init.method = "POST";
        }
        return fetch(url, init).then(function (res) {
            return res.text().then(function (text) {
                var json;
                try { json = JSON.parse(text); }
                catch (e) {
                    // Not JSON: almost always a PHP fatal before our handler
                    // ran. Keep the message short but actionable - the old
                    // "Unexpected server response (500)" told the operator
                    // nothing about where to look.
                    var hint = res.status >= 500
                        ? " - check the LibreNMS log on the host"
                        : "";
                    throw new Error("Server returned HTML instead of JSON (HTTP " +
                        res.status + ")" + hint);
                }
                if (!res.ok || json.status === "error") {
                    var msg = json.message || ("request failed (" + res.status + ")");
                    if (json.log) msg += " — see " + json.log;
                    throw new Error(msg);
                }
                return json.data;
            });
        });
    }

    // -------------------------------------------------------------- notices
    function notice(kind, message) {
        var box = document.getElementById("br-notice");
        if (!box) return;
        var cls = { ok: "alert-success", error: "alert-danger", warn: "alert-warning", info: "alert-info" }[kind] || "alert-info";
        box.className = "alert " + cls;
        box.innerHTML = esc(message);
        box.style.display = "block";
        if (kind === "ok" || kind === "info") {
            setTimeout(function () { box.style.display = "none"; }, 6000);
        }
    }

    function confirmDialog(title, bodyHtml, phrase) {
        return new Promise(function (resolve) {
            var input = el("input", { type: "text", class: "form-control", id: "br-confirm-input" });
            var body = el("div", { html: bodyHtml });
            if (phrase) {
                body.appendChild(el("p", { class: "br-confirm-hint", html: "Type <strong>" + esc(phrase) + "</strong> to confirm:" }));
                body.appendChild(input);
            }
            var modal = el("div", { class: "br-modal-backdrop" }, [
                el("div", { class: "br-modal" }, [
                    el("h4", { text: title }),
                    body,
                    el("div", { class: "br-modal-actions" }, [
                        el("button", { class: "btn btn-default btn-sm", onclick: function () { close(false); } }, ["Cancel"]),
                        el("button", {
                            class: "btn btn-danger btn-sm",
                            onclick: function () {
                                if (phrase && input.value.trim() !== phrase) {
                                    input.style.borderColor = "#d9534f";
                                    return;
                                }
                                close(true);
                            }
                        }, ["Confirm"])
                    ])
                ])
            ]);
            function close(result) {
                modal.remove();
                resolve(result);
            }
            document.body.appendChild(modal);
            if (input) input.focus();
        });
    }

    function progressBar(percent, stage) {
        percent = Math.max(0, Math.min(100, Number(percent || 0)));
        return el("div", { class: "br-progress" }, [
            el("div", { class: "br-progress-label" }, [
                el("span", { text: stage || "" }),
                el("span", { text: percent + "%" })
            ]),
            el("div", { class: "br-progress-track" }, [
                el("div", { class: "br-progress-fill", style: "width:" + percent + "%" })
            ])
        ]);
    }

    // ----------------------------------------------------------------- tabs
    var TABS = [
        { id: "dashboard", label: "Dashboard", icon: "fa-dashboard" },
        { id: "manage", label: "Backup Management", icon: "fa-archive" },
        { id: "history", label: "Backup History", icon: "fa-history" },
        { id: "restore", label: "Restore", icon: "fa-undo" },
        { id: "scheduler", label: "Scheduler", icon: "fa-clock-o" },
        { id: "settings", label: "Settings", icon: "fa-cogs" },
        { id: "audit", label: "Audit Log", icon: "fa-list-alt" }
    ];

    function renderShell() {
        root.innerHTML = "";
        var header = el("div", { class: "br-header" }, [
            el("div", { class: "br-title" }, [
                el("h2", { html: '<i class="fa fa-database"></i> Administration &rsaquo; Backup &amp; Restore' }),
                el("p", { class: "br-sub", text: "Encrypted, verifiable backups and disaster recovery for LibreNMS" })
            ]),
            el("div", { class: "br-header-actions" }, [
                el("span", { class: "label label-default", text: "v" + (B.version || "1.0.0") }),
                el("button", {
                    class: "btn btn-primary",
                    onclick: function () { runBackup("full"); },
                    html: '<i class="fa fa-play"></i> Run Full Backup Now'
                })
            ])
        ]);

        var nav = el("ul", { class: "br-tabs" }, TABS.map(function (t) {
            return el("li", {
                class: state.tab === t.id ? "active" : "",
                onclick: function () { go(t.id); }
            }, [el("a", { html: '<i class="fa ' + t.icon + '"></i> ' + esc(t.label) })]);
        }));

        root.appendChild(header);
        root.appendChild(el("div", { id: "br-notice", class: "alert", style: "display:none" }));
        root.appendChild(nav);
        root.appendChild(el("div", { id: "br-panel", class: "br-panel" }, [
            el("div", { class: "br-loading" }, [el("i", { class: "fa fa-circle-o-notch fa-spin fa-2x" })])
        ]));
    }

    function go(tab) {
        state.tab = tab;
        if (window.location.hash !== "#" + tab) window.location.hash = tab;
        renderShell();
        var panel = document.getElementById("br-panel");
        var render = {
            dashboard: renderDashboard,
            manage: renderManage,
            history: renderHistory,
            restore: renderRestore,
            scheduler: renderScheduler,
            settings: renderSettings,
            audit: renderAudit
        }[tab] || renderDashboard;
        render(panel);
    }

    // ------------------------------------------------------------ dashboard
    function renderDashboard(panel) {
        api("status").then(function (d) {
            panel.innerHTML = "";
            var cards = el("div", { class: "br-cards" });
            function card(label, value, sub, kind) {
                return el("div", { class: "br-card " + (kind || "") }, [
                    el("div", { class: "br-card-label", text: label }),
                    el("div", { class: "br-card-value", text: value }),
                    el("div", { class: "br-card-sub", text: sub || "" })
                ]);
            }
            cards.appendChild(card("Total backups", d.totals.backups,
                d.totals.verified + " verified \u00b7 " + d.totals.failed + " failed"));
            cards.appendChild(card("Last successful backup",
                d.last_backup ? when(d.last_backup.created_at) : "never",
                d.last_backup ? d.last_backup.backup_ref + " \u00b7 " + bytes(d.last_backup.size) : "no backups yet",
                d.last_backup ? "" : "warn"));
            cards.appendChild(card("Last failed backup",
                d.last_failed_backup ? when(d.last_failed_backup.created_at) : "none",
                d.last_failed_backup ? (d.last_failed_backup.error || "").slice(0, 60) : "healthy",
                d.last_failed_backup ? "danger" : ""));
            cards.appendChild(card("Next scheduled backup",
                d.next_scheduled ? d.next_scheduled.at : "not configured",
                d.next_scheduled ? d.next_scheduled.name + " (" + d.next_scheduled.frequency + ")" : "set a schedule",
                d.next_scheduled ? "" : "warn"));
            cards.appendChild(card("Backup storage used", bytes(d.totals.storage_bytes),
                d.storage.root));
            cards.appendChild(card("Available disk space", d.storage.free_human,
                d.storage.percent_used !== null ? d.storage.percent_used + "% of volume used" : "",
                d.storage.percent_used > 90 ? "danger" : ""));
            cards.appendChild(card("Last restore",
                d.last_restore ? d.last_restore.state : "never",
                d.last_restore ? when(d.last_restore.created_at) : "",
                d.last_restore && d.last_restore.state === "COMPLETED" ? "" : "warn"));
            panel.appendChild(cards);

            if (d.current_job) {
                panel.appendChild(el("div", { class: "br-section" }, [
                    el("h4", { text: "Current job" }),
                    el("p", { text: d.current_job.kind + " \u00b7 " + d.current_job.status + " \u00b7 " + (d.current_job.message || "") }),
                    progressBar(d.current_job.progress, d.current_job.stage),
                    el("button", { class: "btn btn-default btn-sm", onclick: function () { watchJob(d.current_job.id); } },
                        ["View live log"])
                ]));
            }

            panel.appendChild(el("div", { class: "br-section" }, [
                el("h4", { text: "System status" }),
                el("ul", { class: "br-kv" }, [
                    el("li", { html: "Privileged helper: <strong>" + esc(d.worker.helper) + "</strong>" }),
                    el("li", { html: "Encryption: <strong>" + esc(d.encryption.scheme) + "</strong> (key " + esc(String(d.encryption.key_fingerprint).slice(0, 16)) + ")" }),
                    el("li", { html: "Worker heartbeat: <strong>" + esc(d.worker.last_heartbeat || "none yet") + "</strong>" })
                ])
            ]));

            if (d.current_job) schedulePoll();
        }).catch(function (e) {
            panel.innerHTML = '<div class="alert alert-danger">Could not load dashboard: ' + esc(e.message) + "</div>";
        });
    }

    // -------------------------------------------------------------- backups
    function runBackup(type) {
        if (state.busy) return notice("warn", "A job is already being queued.");
        if (type === "full") {
            confirmDialog("Run full backup now?",
                "<p>This creates an encrypted archive of the database, RRD data, application " +
                "configuration, system configuration and integrations.</p>" +
                "<p>Polling continues during the database dump (single-transaction).</p>"
            ).then(function (ok) {
                if (ok) queueBackup("full");
            });
            return;
        }
        queueBackup(type);
    }

    function queueBackup(type) {
        state.busy = true;
        api("backups", { method: "POST", body: { type: type } }).then(function (d) {
            state.busy = false;
            notice("ok", "Backup queued: " + d.job.id);
            watchJob(d.job.id);
        }).catch(function (e) {
            state.busy = false;
            notice("error", e.message);
        });
    }

    function renderManage(panel) {
        panel.innerHTML = "";
        panel.appendChild(el("div", { class: "br-section" }, [
            el("h4", { text: "Create a backup" }),
            el("p", { class: "br-sub", text: "Choose a component set. Full = database + RRD + application + system + integrations." }),
            el("div", { class: "br-buttons" }, [
                el("button", { class: "btn btn-primary", onclick: function () { runBackup("full"); } }, ["Full backup"]),
                el("button", { class: "btn btn-default", onclick: function () { queueBackup("database"); } }, ["Database only"]),
                el("button", { class: "btn btn-default", onclick: function () { queueBackup("rrd"); } }, ["RRD only"]),
                el("button", { class: "btn btn-default", onclick: function () { queueBackup("configuration"); } }, ["Configuration"]),
                el("button", { class: "btn btn-default", onclick: function () { queueBackup("system"); } }, ["System configuration"])
            ])
        ]));
        var live = el("div", { id: "br-live" });
        panel.appendChild(live);
        loadJobs(live);

        var recent = el("div", { class: "br-section" });
        panel.appendChild(recent);
        api("jobs").then(function (d) {
            recent.appendChild(el("h4", { text: "Recent jobs" }));
            if (!d.items.length) {
                recent.appendChild(el("p", { class: "br-sub", text: "No jobs yet." }));
                return;
            }
            recent.appendChild(jobsTable(d.items));
        }).catch(function (e) {
            recent.appendChild(el("div", { class: "alert alert-danger", text: e.message }));
        });
    }

    function loadJobs(container) {
        api("jobs").then(function (d) {
            container.innerHTML = "";
            var running = d.items.filter(function (j) { return j.status === "running" || j.status === "queued"; })[0];
            if (running) {
                container.appendChild(el("div", { class: "br-section" }, [
                    el("h4", { text: "In progress \u2014 " + running.kind }),
                    progressBar(running.progress, running.stage),
                    el("p", { class: "br-sub", text: running.message || "" }),
                    el("button", { class: "btn btn-default btn-sm", onclick: function () { watchJob(running.id); } }, ["Live log"])
                ]));
                schedulePoll();
            }
        }).catch(function () { /* dashboard already reports errors */ });
    }

    function jobsTable(items) {
        var tbody = el("tbody");
        items.forEach(function (j) {
            tbody.appendChild(el("tr", {}, [
                el("td", { html: "<code>" + esc(j.kind) + "</code>" }),
                el("td", { text: j.type || "\u2014" }),
                el("td", {}, [
                    el("span", { class: "br-status br-status-" + esc(j.status), text: j.status }),
                    el("div", { class: "br-mini-bar" }, [
                        el("div", { style: "width:" + (j.progress || 0) + "%", class: "br-mini-fill" })
                    ])
                ]),
                el("td", { text: when(j.created_at) }),
                el("td", { text: j.actor || "" }),
                el("td", { class: "br-ellipsis", text: j.error || j.message || "" })
            ]));
        });
        return el("table", { class: "table table-condensed table-hover" }, [
            el("thead", {}, [el("tr", {}, [
                el("th", { text: "Kind" }), el("th", { text: "Type" }), el("th", { text: "Status" }),
                el("th", { text: "Created" }), el("th", { text: "Actor" }), el("th", { text: "Detail" })
            ])]),
            tbody
        ]);
    }

    function watchJob(jobId) {
        state.jobId = jobId;
        var panel = el("div", { class: "br-section" }, [
            el("h4", { text: "Job " + jobId }),
            el("div", { id: "br-job-progress" }, [progressBar(0, "starting")]),
            el("pre", { class: "br-log", id: "br-job-log" }, ["waiting for worker output\u2026"])
        ]);
        var host = document.getElementById("br-live") || document.getElementById("br-panel");
        host.innerHTML = "";
        host.appendChild(panel);
        pollJob();
    }

    function pollJob() {
        if (state.timer) clearTimeout(state.timer);
        if (!state.jobId) return;
        api("jobs/" + state.jobId).then(function (d) {
            var p = document.getElementById("br-job-progress");
            if (p) {
                p.innerHTML = "";
                p.appendChild(progressBar(d.job.progress, d.job.stage || d.job.status));
            }
            var log = document.getElementById("br-job-log");
            if (log) {
                log.textContent = (d.log || []).map(function (l) {
                    return (l.ts || "").replace("T", " ").replace("Z", "") + "  " +
                        String(l.progress !== null && l.progress !== undefined ? "[" + l.progress + "%] " : "") +
                        l.message;
                }).join("\n") || "no log output yet";
                log.scrollTop = log.scrollHeight;
            }
            if (d.job.status === "completed" || d.job.status === "failed") {
                state.jobId = null;
                notice(d.job.status === "completed" ? "ok" : "error",
                    "Job " + d.job.status + (d.job.error ? ": " + d.job.error : ""));
                return;
            }
            state.timer = setTimeout(pollJob, 2500);
        }).catch(function (e) {
            notice("error", e.message);
            state.jobId = null;
        });
    }

    function schedulePoll() {
        if (state.timer) clearTimeout(state.timer);
        state.timer = setTimeout(function () {
            if (state.tab === "dashboard") go("dashboard");
            else if (state.tab === "manage") go("manage");
            else if (state.tab === "restore") go("restore");
        }, 5000);
    }

    // -------------------------------------------------------------- history
    function renderHistory(panel) {
        api("backups?limit=100").then(function (d) {
            panel.innerHTML = "";
            panel.appendChild(el("h4", { text: "Backup history (" + d.count + ")" }));
            if (!d.items.length) {
                panel.appendChild(el("p", { class: "br-sub", text: "No backups recorded yet." }));
                return;
            }
            var tbody = el("tbody");
            d.items.forEach(function (b) {
                var actions = el("td", { class: "br-actions" }, [
                    el("button", { class: "btn btn-xs btn-default", onclick: function () { showDetail(b.id); } }, ["Details"]),
                    el("button", {
                        class: "btn btn-xs btn-default",
                        onclick: function () { verifyBackup(b.id); },
                        disabled: b.status !== "completed" ? "disabled" : null
                    }, ["Verify"]),
                    el("a", {
                        class: "btn btn-xs btn-default",
                        href: B.endpoint + "?r=" + encodeURIComponent("backups/" + b.id + "/download") +
                              "&_token=" + encodeURIComponent(B.csrf),
                        onclick: function (ev) {
                            // Download is a POST-guarded stream; use a synthetic form.
                            ev.preventDefault();
                            window.location = B.endpoint + "?r=" +
                                encodeURIComponent("backups/" + b.id + "/download") +
                                "&_token=" + encodeURIComponent(B.csrf);
                        }
                    }, ["Download"]),
                    el("button", {
                        class: "btn btn-xs btn-warning",
                        onclick: function () { go("restore"); },
                        disabled: b.status !== "completed" || b.verification !== "verified" ? "disabled" : null
                    }, ["Restore"]),
                    el("button", { class: "btn btn-xs btn-danger", onclick: function () { deleteBackup(b.id); } }, ["Delete"])
                ]);
                tbody.appendChild(el("tr", {}, [
                    el("td", { html: "<code>" + esc(b.backup_ref) + "</code>" }),
                    el("td", { text: b.type }),
                    el("td", { text: b.created_at }),
                    el("td", { text: b.size_human }),
                    el("td", { text: b.destination }),
                    el("td", {}, [el("span", { class: "br-status br-status-" + esc(b.status), text: b.status })]),
                    el("td", {}, [
                        el("span", {
                            class: "br-status br-status-" + (b.verification === "verified" ? "verified" : (b.verification === "failed" ? "failed" : "pending")),
                            text: b.verification
                        }),
                        b.encrypted ? el("i", { class: "fa fa-lock br-lock", title: "encrypted (" + (b.encryption || "") + ")" }) : null
                    ]),
                    actions
                ]));
            });
            panel.appendChild(el("div", { class: "table-responsive" }, [
                el("table", { class: "table table-condensed table-hover br-table" }, [
                    el("thead", {}, [el("tr", {}, [
                        el("th", { text: "Backup ID" }), el("th", { text: "Type" }), el("th", { text: "Date" }),
                        el("th", { text: "Size" }), el("th", { text: "Destination" }), el("th", { text: "Status" }),
                        el("th", { text: "Verification" }), el("th", { text: "Actions" })
                    ])]),
                    tbody
                ])
            ]));
        }).catch(function (e) {
            panel.innerHTML = '<div class="alert alert-danger">' + esc(e.message) + "</div>";
        });
    }

    function showDetail(ref) {
        api("backups/" + ref).then(function (d) {
            var m = d.manifest || {};
            var rows = [
                ["Backup ID", d.backup.backup_ref], ["Type", d.backup.type],
                ["Created", d.backup.created_at], ["Size", d.backup.size_human],
                ["Encryption", d.backup.encryption], ["Verified", d.backup.verification],
                ["LibreNMS", d.backup.librenms_version || "unknown"],
                ["Worker", d.backup.worker_version || "unknown"],
                ["Components", (d.backup.components || []).join(", ")],
                ["Archive", m.archive ? m.archive.file + " (" + m.archive.compression + ")" : "manifest sidecar missing"],
                ["Restorable", d.can_restore ? "yes" : "no"]
            ];
            var html = "<table class='table table-condensed'>" + rows.map(function (r) {
                return "<tr><th>" + esc(r[0]) + "</th><td>" + esc(r[1] || "\u2014") + "</td></tr>";
            }).join("") + "</table>";
            confirmDialog("Backup " + ref, html, null);
        }).catch(function (e) { notice("error", e.message); });
    }

    function verifyBackup(ref) {
        api("backups/" + ref + "/verify", { method: "POST", body: {} }).then(function (d) {
            notice("ok", "Verification started for " + ref);
            watchJob(d.job.id);
        }).catch(function (e) { notice("error", e.message); });
    }

    function deleteBackup(ref) {
        confirmDialog("Delete backup " + ref + "?",
            "<p>This permanently removes the encrypted archive and its checksum files. " +
            "Backups referenced by a recovery checkpoint are protected and will be refused.</p>",
            ref
        ).then(function (ok) {
            if (!ok) return;
            api("backups/" + ref + "/delete", { method: "POST", body: { confirm: ref } })
                .then(function (d) { notice("ok", "Deletion queued: " + d.job.id); watchJob(d.job.id); })
                .catch(function (e) { notice("error", e.message); });
        });
    }

    // -------------------------------------------------------------- restore
    function renderRestore(panel) {
        Promise.all([api("restores"), api("backups?limit=50")]).then(function (res) {
            var restores = res[0], backups = res[1];
            panel.innerHTML = "";

            panel.appendChild(el("div", { class: "br-warning" }, [
                el("strong", { text: "Restoration replaces live data. " }),
                el("span", { text: "A verified recovery checkpoint of the current system is created before anything is modified. The checkpoint is never deleted automatically." })
            ]));

            if (restores.active) {
                panel.appendChild(el("div", { class: "br-section" }, [
                    el("h4", { text: "Restore in progress \u2014 " + restores.active.state }),
                    el("p", { class: "br-sub", text: "Checkpoint: " + (restores.active.checkpoint || "pending") }),
                    el("pre", { class: "br-log", text: restores.active.log || "starting\u2026" })
                ]));
                schedulePoll();
            } else {
                var candidates = backups.items.filter(function (b) {
                    return b.status === "completed" && b.verification === "verified";
                });
                var select = el("select", { class: "form-control", id: "br-restore-target" });
                candidates.forEach(function (b) {
                    select.appendChild(el("option", { value: b.id }, [b.backup_ref + " \u2014 " + b.created_at + " \u2014 " + b.size_human]));
                });
                panel.appendChild(el("div", { class: "br-section" }, [
                    el("h4", { text: "Start a restoration" }),
                    candidates.length ? el("div", {}, [
                        el("label", { text: "Restore from" }),
                        select,
                        el("button", {
                            class: "btn btn-danger", style: "margin-top:10px",
                            onclick: function () { preflightRestore(select.value); }
                        }, ["Run pre-flight checks"])
                    ]) : el("p", { class: "br-sub", text: "No verified backups available. Create and verify a backup first." })
                ]));
            }

            if (restores.checkpoints.length) {
                var cpBody = el("tbody");
                restores.checkpoints.forEach(function (c) {
                    cpBody.appendChild(el("tr", {}, [
                        el("td", { html: "<code>" + esc(c.id) + "</code>" }),
                        el("td", { text: c.label || "" }),
                        el("td", { text: bytes(c.size) }),
                        el("td", { text: when(c.created_at) }),
                        el("td", {}, [
                            el("button", {
                                class: "btn btn-xs btn-warning",
                                onclick: function () { restoreCheckpoint(c.id); }
                            }, ["Restore this checkpoint"])
                        ])
                    ]));
                });
                panel.appendChild(el("div", { class: "br-section" }, [
                    el("h4", { text: "Recovery checkpoints" }),
                    el("table", { class: "table table-condensed" }, [
                        el("thead", {}, [el("tr", {}, [
                            el("th", { text: "Checkpoint" }), el("th", { text: "Label" }),
                            el("th", { text: "Size" }), el("th", { text: "Created" }), el("th", { text: "" })
                        ])]),
                        cpBody
                    ])
                ]));
            }

            if (restores.items.length) {
                var rBody = el("tbody");
                restores.items.forEach(function (r) {
                    rBody.appendChild(el("tr", {}, [
                        el("td", { html: "<code>" + esc(r.id.slice(0, 8)) + "</code>" }),
                        el("td", { text: r.backup_ref }),
                        el("td", {}, [el("span", { class: "br-status br-status-" + esc((r.state || "").toLowerCase()), text: r.state })]),
                        el("td", { text: when(r.created_at) }),
                        el("td", { class: "br-ellipsis", text: r.error || "" })
                    ]));
                });
                panel.appendChild(el("div", { class: "br-section" }, [
                    el("h4", { text: "Restore history" }),
                    el("table", { class: "table table-condensed" }, [
                        el("thead", {}, [el("tr", {}, [
                            el("th", { text: "Restore" }), el("th", { text: "Backup" }),
                            el("th", { text: "State" }), el("th", { text: "Started" }), el("th", { text: "Error" })
                        ])]),
                        rBody
                    ])
                ]));
            }
        }).catch(function (e) {
            panel.innerHTML = '<div class="alert alert-danger">' + esc(e.message) + "</div>";
        });
    }

    function preflightRestore(ref) {
        api("restores/preflight/" + encodeURIComponent(ref)).then(function (d) {
            var html = "<ul class='br-checklist'>" + d.checks.map(function (c) {
                return "<li class='" + (c.ok ? "ok" : "bad") + "'><i class='fa " +
                    (c.ok ? "fa-check-circle" : "fa-times-circle") + "'></i> " +
                    esc(c.name.replace(/_/g, " ")) + " <span class='br-sub'>" + esc(c.detail) + "</span></li>";
            }).join("") + "</ul>";
            if (!d.ready) {
                html += "<div class='alert alert-danger' style='margin-top:10px'>Pre-flight checks failed. " +
                    "Resolve the items marked in red before restoring.</div>";
                confirmDialog("Pre-flight failed", html, null);
                return;
            }
            html += "<p><strong>This will:</strong> create a recovery checkpoint, enter maintenance mode, " +
                "replace the database, RRD data and configuration, then validate services.</p>";
            confirmDialog("Restore from " + ref + "?", html, d.confirm_phrase).then(function (ok) {
                if (!ok) return;
                api("restores", { method: "POST", body: { backup_ref: ref, confirm: d.confirm_phrase } })
                    .then(function (r) { notice("ok", "Restore queued: " + r.restore.id); watchJob(r.job.id); })
                    .catch(function (e) { notice("error", e.message); });
            });
        }).catch(function (e) { notice("error", e.message); });
    }

    function restoreCheckpoint(id) {
        api("settings").then(function (s) {
            var phrase = s.plugin.confirm_phrase || "RESTORE";
            confirmDialog("Restore recovery checkpoint " + id + "?",
                "<p>This restores the system state captured immediately before a previous restore. " +
                "A new checkpoint of the current state is taken first.</p>", phrase
            ).then(function (ok) {
                if (!ok) return;
                api("restores/checkpoint", { method: "POST", body: { checkpoint: id, confirm: phrase } })
                    .then(function (r) { notice("ok", "Checkpoint restore queued"); watchJob(r.job.id); })
                    .catch(function (e) { notice("error", e.message); });
            });
        });
    }

    // ------------------------------------------------------------ scheduler
    function renderScheduler(panel) {
        api("schedules").then(function (d) {
            panel.innerHTML = "";
            panel.appendChild(el("p", { class: "br-sub", html:
                "Schedules are executed by the dedicated <code>librenms-backup-scheduler.timer</code> " +
                "systemd timer, which is independent of the LibreNMS scheduler and polling jobs." }));

            var current = d.items[0];
            var form = el("form", { class: "br-form", onsubmit: function (ev) { ev.preventDefault(); saveSchedule(form, current); } });
            function field(label, node) {
                return el("div", { class: "br-field" }, [el("label", { text: label }), node]);
            }
            var name = el("input", { class: "form-control", name: "name", required: "required", value: current ? current.name : "Nightly full backup" });
            var freq = el("select", { class: "form-control", name: "frequency" });
            ["daily", "weekly", "monthly"].forEach(function (f) {
                var o = el("option", { value: f }, [f]);
                if (current && current.frequency === f) o.selected = true;
                freq.appendChild(o);
            });
            var time = el("input", { class: "form-control", name: "time", type: "time", required: "required", value: current ? current.time : "02:30" });
            var weekday = el("select", { class: "form-control", name: "weekday" });
            ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"].forEach(function (d2, i) {
                var o = el("option", { value: i }, [d2]);
                if (current && Number(current.weekday) === i) o.selected = true;
                weekday.appendChild(o);
            });
            var dom = el("input", { class: "form-control", name: "day_of_month", type: "number", min: 1, max: 28, value: current && current.day_of_month ? current.day_of_month : 1 });
            var type = el("select", { class: "form-control", name: "backup_type" });
            ["full", "database", "rrd", "configuration", "system"].forEach(function (t) {
                var o = el("option", { value: t }, [t]);
                if (current && current.type === t) o.selected = true;
                type.appendChild(o);
            });
            var enabled = el("select", { class: "form-control", name: "enabled" }, [
                el("option", { value: "1" }, ["Enabled"]), el("option", { value: "0" }, ["Disabled"])
            ]);
            if (current && !current.enabled) enabled.value = "0";
            var kd = el("input", { class: "form-control", name: "keep_daily", type: "number", min: 0, value: current ? current.retention.keep_daily : 7 });
            var kw = el("input", { class: "form-control", name: "keep_weekly", type: "number", min: 0, value: current ? current.retention.keep_weekly : 4 });
            var km = el("input", { class: "form-control", name: "keep_monthly", type: "number", min: 0, value: current ? current.retention.keep_monthly : 6 });

            form.appendChild(field("Schedule name", name));
            form.appendChild(field("Frequency", freq));
            form.appendChild(field("Run at (server time)", time));
            form.appendChild(field("Weekday (weekly only)", weekday));
            form.appendChild(field("Day of month (monthly only, 1-28)", dom));
            form.appendChild(field("Backup type", type));
            form.appendChild(field("State", enabled));
            form.appendChild(field("Keep daily", kd));
            form.appendChild(field("Keep weekly", kw));
            form.appendChild(field("Keep monthly", km));
            form.appendChild(el("button", { class: "btn btn-primary", type: "submit" }, ["Save schedule"]));
            panel.appendChild(form);

            panel.appendChild(el("div", { class: "br-section" }, [
                el("h4", { text: "Timer state" }),
                el("p", { html: "Unit <code>" + esc(d.timer_state.unit) + "</code>: <strong>" + esc(d.timer_state.active) + "</strong>" }),
                current && current.next_run ? el("p", { text: "Next run: " + current.next_run }) : null
            ]));
        }).catch(function (e) {
            panel.innerHTML = '<div class="alert alert-danger">' + esc(e.message) + "</div>";
        });
    }

    function saveSchedule(form, current) {
        var fd = new FormData(form);
        var body = {
            name: fd.get("name"), frequency: fd.get("frequency"), time: fd.get("time"),
            weekday: fd.get("weekday"), day_of_month: fd.get("day_of_month"),
            enabled: fd.get("enabled"), backup_type: fd.get("backup_type"),
            keep_daily: fd.get("keep_daily"), keep_weekly: fd.get("keep_weekly"),
            keep_monthly: fd.get("keep_monthly")
        };
        if (current) body.schedule_id = current.id;
        api("schedules", { method: "POST", body: body })
            .then(function () { notice("ok", "Schedule saved"); go("scheduler"); })
            .catch(function (e) { notice("error", e.message); });
    }

    // ------------------------------------------------------------- settings
    function renderSettings(panel) {
        api("settings").then(function (d) {
            panel.innerHTML = "";
            var cfg = d.worker_config || {};
            panel.appendChild(el("div", { class: "br-section" }, [
                el("h4", { text: "Worker configuration (read-only)" }),
                el("p", { class: "br-sub", html: "Root-owned file <code>" + esc(cfg.path || "") + "</code>. " +
                    "Encryption keys, database credentials and paths are managed there by the installer, never by the GUI." }),
                el("ul", { class: "br-kv" }, Object.keys(cfg).filter(function (k) {
                    return k !== "path" && k !== "readable";
                }).map(function (k) {
                    return el("li", { html: esc(k) + ": <strong>" + esc(cfg[k]) + "</strong>" });
                }))
            ]));

            panel.appendChild(el("div", { class: "br-section" }, [
                el("h4", { text: "Storage" }),
                el("ul", { class: "br-kv" }, [
                    el("li", { html: "Backup directory: <code>" + esc(d.storage.root) + "</code>" }),
                    el("li", { html: "Archive bytes: <strong>" + bytes(d.storage.archive_bytes) + "</strong>" }),
                    el("li", { html: "Free space: <strong>" + esc(d.storage.free_human) + "</strong>" })
                ])
            ]));

            var form = el("form", { class: "br-form", onsubmit: function (ev) { ev.preventDefault(); saveSettings(form); } });
            function select(name, value) {
                var s = el("select", { class: "form-control", name: name }, [
                    el("option", { value: "1" }, ["Enabled"]), el("option", { value: "0" }, ["Disabled"])
                ]);
                s.value = value ? "1" : "0";
                return s;
            }
            form.appendChild(el("div", { class: "br-field" }, [el("label", { text: "Plugin enabled" }), select("enabled", d.plugin.enabled)]));
            form.appendChild(el("div", { class: "br-field" }, [el("label", { text: "Require typed confirmation" }), select("require_typed_confirmation", d.plugin.require_typed_confirmation)]));
            form.appendChild(el("div", { class: "br-field" }, [el("label", { text: "Notify on failure" }), select("notify_on_failure", d.plugin.notify_on_failure)]));
            form.appendChild(el("div", { class: "br-field" }, [el("label", { text: "Keep daily" }), el("input", { class: "form-control", type: "number", name: "retention_keep_daily", value: 7, min: 0 })]));
            form.appendChild(el("div", { class: "br-field" }, [el("label", { text: "Keep weekly" }), el("input", { class: "form-control", type: "number", name: "retention_keep_weekly", value: 4, min: 0 })]));
            form.appendChild(el("div", { class: "br-field" }, [el("label", { text: "Keep monthly" }), el("input", { class: "form-control", type: "number", name: "retention_keep_monthly", value: 6, min: 0 })]));
            var enc = el("select", { class: "form-control", name: "encryption_scheme" });
            ["age", "aesgcm-pbkdf2", "none"].forEach(function (s) {
                var o = el("option", { value: s }, [s]);
                if (cfg.encryption === s) o.selected = true;
                enc.appendChild(o);
            });
            form.appendChild(el("div", { class: "br-field" }, [
                el("label", { text: "Encryption scheme (must match br.conf)" }), enc,
                el("p", { class: "br-sub", text: "Changing this only updates the GUI record; the effective scheme is read from br.conf by the worker." })
            ]));
            form.appendChild(el("button", { class: "btn btn-primary", type: "submit" }, ["Save settings"]));
            panel.appendChild(form);
        }).catch(function (e) {
            panel.innerHTML = '<div class="alert alert-danger">' + esc(e.message) + "</div>";
        });
    }

    function saveSettings(form) {
        var fd = new FormData(form);
        var body = {};
        ["enabled", "require_typed_confirmation", "notify_on_failure", "retention_keep_daily",
            "retention_keep_weekly", "retention_keep_monthly", "encryption_scheme"].forEach(function (k) {
            body[k] = fd.get(k);
        });
        api("settings", { method: "POST", body: body })
            .then(function () { notice("ok", "Settings saved"); })
            .catch(function (e) { notice("error", e.message); });
    }

    // ---------------------------------------------------------------- audit
    function renderAudit(panel) {
        api("audit").then(function (d) {
            panel.innerHTML = "";
            panel.appendChild(el("h4", { text: "Administrative audit log" }));
            if (!d.items.length) {
                panel.appendChild(el("p", { class: "br-sub", text: "No audit events yet." }));
                return;
            }
            var tbody = el("tbody");
            d.items.forEach(function (e) {
                tbody.appendChild(el("tr", {}, [
                    el("td", { text: when(e.at) }),
                    el("td", { text: e.actor }),
                    el("td", { html: "<code>" + esc(e.action) + "</code>" }),
                    el("td", {}, [el("span", { class: "br-status br-status-" + (e.result === "denied" ? "failed" : "completed"), text: e.result })]),
                    el("td", { text: e.ip || "" }),
                    el("td", { class: "br-ellipsis", text: e.detail || "" })
                ]));
            });
            panel.appendChild(el("div", { class: "table-responsive" }, [
                el("table", { class: "table table-condensed table-hover br-table" }, [
                    el("thead", {}, [el("tr", {}, [
                        el("th", { text: "When" }), el("th", { text: "Actor" }), el("th", { text: "Action" }),
                        el("th", { text: "Result" }), el("th", { text: "IP" }), el("th", { text: "Detail" })
                    ])]),
                    tbody
                ])
            ]));
        }).catch(function (e) {
            panel.innerHTML = '<div class="alert alert-danger">' + esc(e.message) + "</div>";
        });
    }

    // ----------------------------------------------------------------- boot
    function boot() {
        root = document.querySelector(".br-app");
        if (!root) return;
        var hash = (window.location.hash || "#dashboard").slice(1);
        state.tab = TABS.filter(function (t) { return t.id === hash; }).length ? hash : "dashboard";
        if (B.role !== "admin") {
            root.innerHTML = '<div class="alert alert-danger">' +
                "You need the LibreNMS <strong>admin</strong> role to use Backup &amp; Restore.</div>";
            return;
        }
        go(state.tab);
        window.addEventListener("hashchange", function () {
            var t = (window.location.hash || "#dashboard").slice(1);
            if (t !== state.tab && TABS.filter(function (x) { return x.id === t; }).length) go(t);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();
