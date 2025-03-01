from operator import attrgetter
import itertools
import logging
import apache_beam as beam
import tenacity
import psycopg2

import config_utils
from crowdtangle import db_functions

logger = logging.getLogger()

def dedupe_account_records_by_max_updated_field(account_records):
    """Return list of account records deduped by ID. If multiple records with the same ID are found
    the record with the highest/latest |updated| is returned.
    """
    account_id_to_latest_updated_record = {}
    for account in account_records:
        if account.id in account_id_to_latest_updated_record:
            account_id_to_latest_updated_record[account.id] = max(
                account, account_id_to_latest_updated_record[account.id], key=attrgetter('updated'))
        else:
            account_id_to_latest_updated_record[account.id] = account
    return list(account_id_to_latest_updated_record.values())

def get_account_record_list_only_latest_updated_records(pcoll):
    """Returns list of account records deduped by max updated field."""
    return dedupe_account_records_by_max_updated_field(
        itertools.chain.from_iterable(map(attrgetter('account_list'), pcoll)))

class WriteCrowdTangleResultsToDatabase(beam.DoFn):
    """DoFn that expects iterables of process_crowdtangle_posts.EncapsulatedPost and writes the
    contained data to database (in order FK relationships reqire).
    """
    def __init__(self, database_connection_params, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._database_connection_params = database_connection_params

    @tenacity.retry(stop=tenacity.stop_after_attempt(3),
                    reraise=True,
                    retry=tenacity.retry_if_exception_type(psycopg2.errors.DeadlockDetected),
                    wait=tenacity.wait_random_exponential(multiplier=1, max=60),
                    before_sleep=tenacity.before_sleep_log(logger, logging.INFO))
    def process(self, pcoll):
        database_connection = config_utils.get_database_connection(self._database_connection_params)
        with database_connection:
            db_interface = db_functions.CrowdTangleDBInterface(database_connection)

            db_interface.upsert_accounts(get_account_record_list_only_latest_updated_records(pcoll))
            db_interface.upsert_posts(itertools.chain(map(attrgetter('post'), pcoll)))
            db_interface.upsert_statistics(
                itertools.chain(map(attrgetter('statistics_actual'), pcoll)),
                itertools.chain(map(attrgetter('statistics_expected'), pcoll)))
            db_interface.upsert_expanded_links(itertools.chain.from_iterable(
                    map(attrgetter('expanded_links'), pcoll)))
            db_interface.upsert_media(itertools.chain.from_iterable(
                    map(attrgetter('media_list'), pcoll)))
            db_interface.insert_post_dashboards({item.post.id: item.dashboard_id for item in pcoll})
