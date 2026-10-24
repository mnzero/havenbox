// Host metadata only. File bytes, credentials and hidden reasoning never belong here.
enum WorkspaceSchema {
    static let version = 2
    static let sql = """
    CREATE TABLE workspaces(id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL CHECK(epoch>0), schema_version INTEGER NOT NULL, state TEXT NOT NULL);
    INSERT INTO workspaces VALUES(1,1,2,'ready');
    CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT NOT NULL, binding_kind TEXT NOT NULL CHECK(binding_kind IN ('agent','toolsOnly')), recipe_revision TEXT, tool_policy_revision TEXT, state TEXT NOT NULL, updated_at INTEGER NOT NULL,
      CHECK((binding_kind='agent' AND recipe_revision IS NOT NULL AND tool_policy_revision IS NULL) OR (binding_kind='toolsOnly' AND recipe_revision IS NULL AND tool_policy_revision IS NOT NULL)));
    CREATE INDEX sessions_order ON sessions(state,updated_at,id);
    CREATE TABLE drafts(session_id TEXT PRIMARY KEY REFERENCES sessions ON DELETE CASCADE, body TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0));
    CREATE TABLE turns(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE, seq INTEGER NOT NULL CHECK(seq>0), idempotency_key TEXT NOT NULL, route_snapshot TEXT, continuation_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL, UNIQUE(session_id,seq), UNIQUE(session_id,idempotency_key), UNIQUE(id,session_id));
    CREATE TRIGGER tools_route BEFORE INSERT ON turns WHEN NEW.route_snapshot IS NOT NULL AND (SELECT binding_kind FROM sessions WHERE id=NEW.session_id)='toolsOnly' BEGIN SELECT RAISE(ABORT,'toolsOnly route'); END;
    CREATE TRIGGER tools_route_update BEFORE UPDATE OF route_snapshot ON turns WHEN NEW.route_snapshot IS NOT NULL AND (SELECT binding_kind FROM sessions WHERE id=NEW.session_id)='toolsOnly' BEGIN SELECT RAISE(ABORT,'toolsOnly route'); END;
    CREATE TABLE messages(id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, session_id TEXT NOT NULL, seq INTEGER NOT NULL CHECK(seq>0), role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')), body TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('sending','complete','failed','interrupted')), UNIQUE(session_id,seq), UNIQUE(id,turn_id), FOREIGN KEY(turn_id,session_id) REFERENCES turns(id,session_id) ON DELETE CASCADE);
    CREATE TABLE plans(id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), turn_id TEXT NOT NULL REFERENCES turns ON DELETE CASCADE, canonical_json TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(id,revision), UNIQUE(id,revision,turn_id,digest));
    CREATE TABLE tasks(id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns ON DELETE CASCADE, plan_id TEXT NOT NULL, plan_revision INTEGER NOT NULL, plan_digest TEXT NOT NULL, snapshot_json TEXT NOT NULL, state TEXT NOT NULL, UNIQUE(id,turn_id,plan_digest), FOREIGN KEY(plan_id,plan_revision,turn_id,plan_digest) REFERENCES plans(id,revision,turn_id,digest));
    CREATE TABLE approvals(id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns ON DELETE CASCADE, task_id TEXT, kind TEXT NOT NULL CHECK(kind IN ('disclosure','execution')), digest TEXT NOT NULL CHECK(length(digest)>0), epoch INTEGER NOT NULL CHECK(epoch>0), expires_at INTEGER NOT NULL, revoked_at INTEGER,
      CHECK((kind='disclosure' AND task_id IS NULL) OR (kind='execution' AND task_id IS NOT NULL)), FOREIGN KEY(task_id,turn_id,digest) REFERENCES tasks(id,turn_id,plan_digest) ON DELETE CASCADE);
    CREATE TABLE provenance(id TEXT PRIMARY KEY, tool_version TEXT NOT NULL, engine_version TEXT NOT NULL, rule_digest TEXT NOT NULL, inputs_manifest TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE file_versions(id TEXT PRIMARY KEY, kind TEXT NOT NULL, object_key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL CHECK(length(sha256)=64), size INTEGER NOT NULL CHECK(size>=0), completeness TEXT NOT NULL CHECK(completeness IN ('partial','complete')), storage_state TEXT NOT NULL CHECK(storage_state IN ('live','deleting','deleted')), provenance_id TEXT REFERENCES provenance ON DELETE RESTRICT);
    CREATE TABLE file_refs(session_id TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE, file_version_id TEXT NOT NULL REFERENCES file_versions ON DELETE RESTRICT, source_session_id TEXT REFERENCES sessions ON DELETE SET NULL, purpose TEXT NOT NULL, PRIMARY KEY(session_id,file_version_id,purpose));
    CREATE TABLE task_files(task_id TEXT NOT NULL REFERENCES tasks ON DELETE CASCADE, file_version_id TEXT NOT NULL REFERENCES file_versions ON DELETE RESTRICT, role TEXT NOT NULL, PRIMARY KEY(task_id,file_version_id,role));
    CREATE TABLE context_items(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE, source_kind TEXT NOT NULL, source_id TEXT NOT NULL, file_version_id TEXT REFERENCES file_versions ON DELETE RESTRICT, selector_json TEXT NOT NULL, body TEXT, hash TEXT NOT NULL, serializer_version INTEGER NOT NULL CHECK(serializer_version>0), UNIQUE(id,session_id));
    CREATE TABLE disclosures(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE, route_digest TEXT NOT NULL, scope_json TEXT NOT NULL, epoch INTEGER NOT NULL, expires_at INTEGER NOT NULL, revoked_at INTEGER, UNIQUE(id,session_id));
    CREATE TABLE disclosure_items(disclosure_id TEXT NOT NULL, context_item_id TEXT NOT NULL, session_id TEXT NOT NULL, content_hash TEXT NOT NULL, PRIMARY KEY(disclosure_id,context_item_id), FOREIGN KEY(disclosure_id,session_id) REFERENCES disclosures(id,session_id) ON DELETE CASCADE, FOREIGN KEY(context_item_id,session_id) REFERENCES context_items(id,session_id) ON DELETE CASCADE);
    CREATE TABLE model_receipts(id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns ON DELETE CASCADE, request_digest TEXT NOT NULL, message_id TEXT NOT NULL, response_hash TEXT NOT NULL, status TEXT NOT NULL, FOREIGN KEY(message_id,turn_id) REFERENCES messages(id,turn_id) ON DELETE CASCADE);
    CREATE TABLE file_metadata(file_version_id TEXT PRIMARY KEY REFERENCES file_versions ON DELETE CASCADE, parser_version TEXT NOT NULL, headers_json TEXT NOT NULL, row_count INTEGER NOT NULL CHECK(row_count>=0), column_count INTEGER NOT NULL CHECK(column_count>=0), encoding TEXT NOT NULL);
    CREATE TABLE file_pins(id TEXT PRIMARY KEY, file_version_id TEXT NOT NULL REFERENCES file_versions ON DELETE RESTRICT, owner_kind TEXT NOT NULL CHECK(owner_kind IN ('call','preview','export','transfer')), owner_id TEXT NOT NULL, epoch INTEGER NOT NULL CHECK(epoch>0), state TEXT NOT NULL CHECK(state IN ('active','checking')));
    CREATE TABLE tool_calls(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks ON DELETE CASCADE, step INTEGER NOT NULL CHECK(step>=0), attempt INTEGER NOT NULL CHECK(attempt>=0), arguments_digest TEXT NOT NULL, dispatch_state TEXT NOT NULL, state TEXT NOT NULL, result_ref TEXT, error_code TEXT, UNIQUE(task_id,step,attempt));
    CREATE TABLE harness_sessions(session_id TEXT PRIMARY KEY REFERENCES sessions ON DELETE CASCADE, harness_revision TEXT NOT NULL, native_id TEXT NOT NULL, checkpoint TEXT, restore_strategy TEXT NOT NULL);
    CREATE TABLE config_revisions(id TEXT PRIMARY KEY, object_id TEXT NOT NULL, kind TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0), schema_version INTEGER NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL, UNIQUE(object_id,revision));
    CREATE TABLE compatibility_checks(sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, combination_digest TEXT NOT NULL, suite_version TEXT NOT NULL, status TEXT NOT NULL, evidence_ref TEXT NOT NULL);
    CREATE TABLE events(id TEXT PRIMARY KEY, workspace_epoch INTEGER NOT NULL CHECK(workspace_epoch>0), seq INTEGER NOT NULL CHECK(seq>0), session_id TEXT REFERENCES sessions ON DELETE CASCADE, turn_id TEXT REFERENCES turns ON DELETE CASCADE, type TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(workspace_epoch,seq));
    CREATE TABLE event_counters(epoch INTEGER PRIMARY KEY, next_seq INTEGER NOT NULL CHECK(next_seq>0));
    INSERT INTO event_counters VALUES(1,1);
    CREATE TABLE commands(id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, result TEXT NOT NULL);
    CREATE TABLE export_items(id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, file_version_id TEXT REFERENCES file_versions ON DELETE RESTRICT, artifact_hash TEXT NOT NULL, target_ref TEXT NOT NULL, decision TEXT NOT NULL, state TEXT NOT NULL, error TEXT);
    CREATE TABLE maintenance_jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, phase TEXT NOT NULL, journal TEXT NOT NULL, state TEXT NOT NULL);
    CREATE TABLE active_slot(singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1), turn_id TEXT NOT NULL UNIQUE REFERENCES turns ON DELETE RESTRICT, epoch INTEGER NOT NULL CHECK(epoch>0), state TEXT NOT NULL);
    CREATE TRIGGER immutable_task BEFORE UPDATE OF turn_id,plan_id,plan_revision,plan_digest,snapshot_json ON tasks BEGIN SELECT RAISE(ABORT,'immutable task snapshot'); END;
    CREATE TRIGGER immutable_plan BEFORE UPDATE ON plans BEGIN SELECT RAISE(ABORT,'immutable plan'); END;
    CREATE TRIGGER immutable_config BEFORE UPDATE ON config_revisions BEGIN SELECT RAISE(ABORT,'immutable config'); END;
    CREATE TRIGGER immutable_compatibility BEFORE UPDATE ON compatibility_checks BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
    CREATE TRIGGER immutable_file BEFORE UPDATE OF object_key,sha256,size,provenance_id ON file_versions BEGIN SELECT RAISE(ABORT,'immutable file version'); END;
    """

    /// v1 databases used a compatibility_checks table without a durable sequence. Rebuild it
    /// transactionally so evidence ordering remains monotonic and latest lookups are deterministic.
    static let migrationV1ToV2 = """
    DROP TRIGGER IF EXISTS immutable_compatibility;
    ALTER TABLE compatibility_checks RENAME TO compatibility_checks_v1;
    CREATE TABLE compatibility_checks(sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, combination_digest TEXT NOT NULL, suite_version TEXT NOT NULL, status TEXT NOT NULL, evidence_ref TEXT NOT NULL);
    INSERT INTO compatibility_checks(id, combination_digest, suite_version, status, evidence_ref)
      SELECT id, combination_digest, suite_version, status, evidence_ref FROM compatibility_checks_v1 ORDER BY rowid;
    DROP TABLE compatibility_checks_v1;
    CREATE TRIGGER immutable_compatibility BEFORE UPDATE ON compatibility_checks BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
    UPDATE workspaces SET schema_version=2 WHERE id=1;
    PRAGMA user_version=2;
    """

    static var referenceGuards: String {
        ["file_refs", "task_files", "context_items", "file_pins", "export_items"].map { table in
            ["INSERT", "UPDATE OF file_version_id"].enumerated().map { index, operation in
                """
                CREATE TRIGGER live_\(table)_\(index) BEFORE \(operation) ON \(table)
                WHEN NEW.file_version_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM file_versions WHERE id=NEW.file_version_id AND storage_state='live')
                BEGIN SELECT RAISE(ABORT,'file is not live'); END;
                """
            }.joined(separator: "\n")
        }.joined(separator: "\n")
    }
}
