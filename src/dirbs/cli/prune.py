"""
DIRBS CLI for pruning old monthly_network_triplets data or obsolete classification_state data.

Installed by setuptools as a dirbs-prune console script.
Copyright (c) 2018 Qualcomm Technologies, Inc.

 All rights reserved.



 Redistribution and use in source and binary forms, with or without modification, are permitted (subject to the
 limitations in the disclaimer below) provided that the following conditions are met:


 * Redistributions of source code must retain the above copyright notice, this list of conditions and the following
 disclaimer.

 * Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following
 disclaimer in the documentation and/or other materials provided with the distribution.

 * Neither the name of Qualcomm Technologies, Inc. nor the names of its contributors may be used to endorse or promote
 products derived from this software without specific prior written permission.

 NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE GRANTED BY THIS LICENSE. THIS SOFTWARE IS PROVIDED BY
 THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
 THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
 COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
 OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR
 TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 POSSIBILITY OF SUCH DAMAGE.

"""

import datetime

from dateutil import relativedelta
from psycopg2 import sql
import click

import dirbs.cli.common as common
import dirbs.metadata as metadata
import dirbs.utils as utils
import dirbs.partition_utils as partition_utils


@click.group(no_args_is_help=False)
@common.setup_initial_logging
@click.version_option()
@common.parse_verbosity_option
@common.parse_db_options
@common.parse_statsd_options
@click.option('--curr-date',
              help='Sets current date in YYYYMMDD format for testing. By default, uses system current date.',
              callback=common.validate_date,
              default=None)
@click.pass_context
@common.configure_logging
def cli(ctx, curr_date):
    """DIRBS script to prune obsolete data from the DIRBS Core PostgreSQL database."""
    ctx.obj['CURR_DATE'] = curr_date


@cli.command()
@click.pass_context
@common.unhandled_exception_handler
@common.cli_wrapper(command='dirbs-prune', subcommand='triplets', required_role='dirbs_core_power_user')
def triplets(ctx, config, statsd, logger, run_id, conn, metadata_conn, command, metrics_root, metrics_run_root):
    """Prune old monthly_network_triplets data."""
    curr_date = ctx.obj['CURR_DATE']

    # Store metadata
    metadata.add_optional_job_metadata(metadata_conn, command, run_id,
                                       curr_date=curr_date.isoformat() if curr_date is not None else None,
                                       retention_months=config.retention_config.months_retention)

    if curr_date is None:
        curr_date = datetime.date.today()

    with conn.cursor() as cursor:
        logger.info('Pruning monthly_network_triplets data outside the retention window from database...')
        retention_months = config.retention_config.months_retention
        first_month_to_drop = datetime.date(curr_date.year, curr_date.month, 1) - \
            relativedelta.relativedelta(months=retention_months)
        logger.info('monthly_network_triplets partitions older than {0} will be pruned'
                    .format(first_month_to_drop))

        country_monthly_partitions = utils.child_table_names(conn, 'monthly_network_triplets_country')
        operator_partitions = utils.child_table_names(conn, 'monthly_network_triplets_per_mno')
        operator_monthly_partitions = []
        for op_partition in operator_partitions:
            operator_monthly_partitions.extend(utils.child_table_names(conn, op_partition))

        parent_tbl_names = ['monthly_network_triplets_country', 'monthly_network_triplets_per_mno']
        rows_before = {}
        for tbl in parent_tbl_names:
            logger.debug('Calculating original number of rows in {0} table...'.format(tbl))
            cursor.execute(sql.SQL('SELECT COUNT(*) FROM {0}'.format(tbl)))
            rows_before[tbl] = cursor.fetchone()[0]
            logger.debug('Calculated original number of rows in {0} table'.format(tbl))
            statsd.gauge('{0}.{1}.rows_before'.format(metrics_run_root, tbl), rows_before[tbl])
        metadata.add_optional_job_metadata(metadata_conn, command, run_id, rows_before=rows_before)

        total_rows_pruned = 0
        total_partitions = country_monthly_partitions + operator_monthly_partitions
        for tblname in total_partitions:
            invariants_list = utils.table_invariants_list(conn, [tblname], ['triplet_month', 'triplet_year'])
            assert len(invariants_list) <= 1
            if len(invariants_list) == 0:
                logger.warn('Found empty partition {0}. Dropping...'.format(tblname))
                cursor.execute(sql.SQL("""DROP TABLE {0} CASCADE""").format(sql.Identifier(tblname)))
            else:
                month, year = tuple(invariants_list[0])

                # Check if table year/month is outside the retention window
                if (datetime.date(year, month, 1) < first_month_to_drop):
                    # Calculate number of rows in the partition table
                    cursor.execute(sql.SQL("""SELECT COUNT(*) FROM {0}""").format(sql.Identifier(tblname)))
                    partition_table_rows = cursor.fetchone()[0]
                    total_rows_pruned += partition_table_rows

                    logger.info('Dropping table {0} with {1} rows...'.format(tblname, partition_table_rows))
                    cursor.execute(sql.SQL("""DROP TABLE {0} CASCADE""").format(sql.Identifier(tblname)))
                    logger.info('Dropped table {0}'.format(tblname))

        rows_after = {}
        for tbl in parent_tbl_names:
            logger.debug('Calculating new number of rows in {0} table...'.format(tbl))
            cursor.execute(sql.SQL('SELECT COUNT(*) FROM {0}'.format(tbl)))
            rows_after[tbl] = cursor.fetchone()[0]
            logger.debug('Calculated new number of rows in {0} table'.format(tbl))
            statsd.gauge('{0}.{1}.rows_after'.format(metrics_run_root, tbl), rows_after[tbl])
        metadata.add_optional_job_metadata(metadata_conn, command, run_id, rows_after=rows_after)

        total_rows_before = sum(rows_before.values())
        total_rows_after = sum(rows_after.values())

        assert (total_rows_before - total_rows_after) == total_rows_pruned
        logger.info('Pruned {0:d} rows of monthly_network_triplets data outside the retention window from database'
                    .format(total_rows_pruned))


@cli.command()
@click.pass_context
@common.unhandled_exception_handler
@common.cli_wrapper(command='dirbs-prune', subcommand='classification_state', required_role='dirbs_core_power_user')
def classification_state(ctx, config, statsd, logger, run_id, conn, metadata_conn, command, metrics_root,
                         metrics_run_root):
    """Prune obsolete classification_state data."""
    curr_date = ctx.obj['CURR_DATE']

    # Store metadata
    metadata.add_optional_job_metadata(metadata_conn, command, run_id,
                                       curr_date=curr_date.isoformat() if curr_date is not None else None,
                                       retention_months=config.retention_config.months_retention)

    logger.info('Pruning classification_state table to remove any classification state data related to '
                'obsolete conditions and data with end_date outside the retention window..')

    cond_config_list = [c.label for c in config.conditions]
    retention_months = config.retention_config.months_retention

    if curr_date is None:
        curr_date = datetime.date.today()

    first_month_to_drop = datetime.date(curr_date.year, curr_date.month, 1) - \
        relativedelta.relativedelta(months=retention_months)
    logger.info('Classification state data with end_date earlier than {0} will be '
                'pruned'.format(first_month_to_drop))

    with utils.db_role_setter(conn, role_name='dirbs_core_power_user'), conn.cursor() as cursor:
        logger.debug('Calculating original number of rows in classification_state table...')
        cursor.execute('SELECT COUNT(*) FROM classification_state')
        rows_before = cursor.fetchone()[0]
        logger.debug('Calculated original number of rows in classification_state table')
        statsd.gauge('{0}rows_before'.format(metrics_run_root), rows_before)
        metadata.add_optional_job_metadata(metadata_conn, command, run_id, rows_before=rows_before)

        # Calculate number of rows in the classification table outside retention window
        cursor.execute(sql.SQL("""SELECT COUNT(*)
                                    FROM classification_state
                                   WHERE end_date < %s """), [first_month_to_drop])
        total_rows_out_window_to_prune = cursor.fetchone()[0]
        logger.info('Found {0:d} rows of classification_state table '
                    'with end_date outside the retention window to prune.'.format(total_rows_out_window_to_prune))

        # Calculate number of rows in the classification with conditions no longer existing
        cursor.execute(sql.SQL("""SELECT COUNT(*)
                                    FROM classification_state
                                   WHERE NOT starts_with_prefix(cond_name, %s)"""), [cond_config_list])
        total_rows_no_cond_to_prune = cursor.fetchone()[0]
        logger.info('Found {0:d} rows of classification_state table with conditions '
                    'no longer existing to prune.'.format(total_rows_no_cond_to_prune))

        logger.debug('Re-creating classification_state table...')
        # Basically, we just re-partition the classification_state table to re-create it, passing a src_filter_sql
        # parameter
        num_phys_imei_shards = partition_utils.num_physical_imei_shards(conn)
        src_filter_sql = cursor.mogrify("""WHERE (end_date > %s
                                              OR end_date IS NULL)
                                             AND cond_name LIKE ANY(%s)""",
                                        [first_month_to_drop, cond_config_list])
        partition_utils.repartition_classification_state(conn, num_physical_shards=num_phys_imei_shards,
                                                         src_filter_sql=str(src_filter_sql, encoding=conn.encoding))
        logger.debug('Re-created classification_state table')

        logger.debug('Calculating new number of rows in classification_state table...')
        cursor.execute('SELECT COUNT(*) FROM classification_state')
        rows_after = cursor.fetchone()[0]
        logger.debug('Calculated new number of rows in classification_state table')
        statsd.gauge('{0}rows_after'.format(metrics_run_root), rows_after)
        metadata.add_optional_job_metadata(metadata_conn, command, run_id, rows_after=rows_after)

        logger.info('Pruned {0:d} rows from classification_state table'.format(rows_after - rows_before))
