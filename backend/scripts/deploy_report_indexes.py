"""Preview required indexes, or create only these indexes with explicit --apply.

Uses the existing Firestore SDK and ADC; does not change any documents or IAM.
"""
import argparse
import json
from pathlib import Path
from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore_admin_v1


def index_definition(value):
    index_type = firestore_admin_v1.Index
    fields = []
    for field in value['fields']:
        options = {'field_path': field['fieldPath']}
        if 'arrayConfig' in field:
            options['array_config'] = index_type.IndexField.ArrayConfig.CONTAINS
        else:
            options['order'] = index_type.IndexField.Order[field['order']]
        fields.append(index_type.IndexField(**options))
    return index_type(query_scope=index_type.QueryScope.COLLECTION, fields=fields)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--no-wait', action='store_true', help='Submit all missing indexes without waiting for their builds.')
    args = parser.parse_args()
    for value in (args.project, args.database):
        if not value or '/' in value or any(c.isspace() for c in value):
            parser.error('Project and database must be single resource identifiers.')
    spec = json.loads((Path(__file__).resolve().parent.parent / 'firestore.report-submissions.indexes.json').read_text())
    definitions = [(item, index_definition(item)) for item in spec['indexes']]
    if not args.apply:
        print(f'Validated {len(definitions)} index definitions for {args.project}/{args.database}. No cloud changes made.')
        print('Run with --apply to deploy these indexes using Application Default Credentials.')
        return
    with firestore_admin_v1.FirestoreAdminClient() as client:
        operations = []
        for item, definition in definitions:
            parent = f'projects/{args.project}/databases/{args.database}/collectionGroups/{item["collectionGroup"]}'
            try:
                operation = client.create_index(request={'parent': parent, 'index': definition}, timeout=30)
                operations.append((item, definition, operation))
                print(f'Index build requested: {item["collectionGroup"]} / {[f.field_path for f in definition.fields]}', flush=True)
            except AlreadyExists:
                print(f'Index already exists: {item["collectionGroup"]}; verify READY in the console.', flush=True)
        # Request every independent build before waiting, rather than waiting
        # several minutes per index while the remaining indexes have not started.
        if not args.no_wait:
            for item, definition, operation in operations:
                operation.result(timeout=600)
                print(f'Index ready: {item["collectionGroup"]} / {[f.field_path for f in definition.fields]}', flush=True)


if __name__ == '__main__':
    main()
