"""
Various utilities to handle running Steps from the commandline.
"""

import argparse
import logging
import os
import os.path
import textwrap

from . import _log, config_parser, utilities
from .exceptions import ValidationError
from .step import Step, get_disable_crds_steppars

built_in_configuration_parameters = [
    "debug",
    "verbose",
    "log-level",
    "log-file",
    "log-stream",
]

logger = logging.getLogger(__name__)


def _print_important_message(header, message, no_wrap=None):
    print("-" * 70)
    print(textwrap.fill(header))
    print(
        textwrap.fill(
            message,
            initial_indent="    ",
            subsequent_indent="    ",
        )
    )
    if no_wrap:
        print(no_wrap)
    print("-" * 70)


class FromCommandLine(str):
    """
    We need a way to distinguish between config values that come from
    a config file and those that come from the commandline.  For
    example, configfile paths must be resolved against the location of
    the config file.  Commandline paths must be resolved against the
    current working directory.  By setting all commandline overrides
    as instances of this class, we can later (in `config_parser.py`)
    use isinstance to see where the values came from.
    """


def _print_parser_error(parser, error):
    """
    Print a formatted error message and parser help text.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The argument parser whose help text will be printed.
    error : Exception
        The error object whose message will be displayed.
    """
    _print_important_message("ERROR PARSING CONFIGURATION:", str(error))
    parser.print_help()


def new_step_from_cmdline(args):
    # parse args only to sort out:
    # - cfg_or_class: first argument, could be config file or a class name
    # - --disable-crds-steppars
    # - --save-parameters (how to handle this with call?)
    # - --debug
    # - --log-level --log-file --log-stream (needs to be configured outside call)
    #
    # we can't handle this with call any more so instead we can reproduce
    # some of call here as long as we:
    # - configure the logger
    # - disable crds steppars: can be passed to get_config_from_reference
    # - save parameters: can be used before instance.run
    # - debug: wrap instance.run


def step_from_cmdline(args):
    """
    Create a step from a configuration file and run it.

    Parameters
    ----------
    args : list of str
        Commandline arguments


    Returns
    -------
    step : Step instance
        If the config file has a `class` parameter, or the commandline
        specifies a class, the return value will be as instance of
        that class.

        Any parameters found in the config file or on the commandline
        will be set as member variables on the returned `Step`
        instance.
    """
    parser = argparse.ArgumentParser(
        description="Run an stpipe Step or Pipeline",
        add_help=False,
    )
    parser.add_argument(
        "cfg_file_or_class",
        type=str,
        help="The configuration file or Python class to run",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="When an exception occurs, invoke the Python debugger, pdb",
    )
    parser.add_argument(
        "--save-parameters",
        type=str,
        help="Save step parameters to specified file",
    )
    parser.add_argument(
        "--disable-crds-steppars",
        action="store_true",
        help="Disable retrieval of step parameter references files from CRDS",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Turn on all logging messages",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        type=str,
        help="Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL) "
        "or a numerical logging level. "
        "Ignored if 'verbose' is specified.",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Full path to a file name to record log messages",
    )
    parser.add_argument(
        "--log-stream",
        type=str,
        default="stderr",
        help="Log stream for terminal messages (stdout, stderr, or null).",
    )

    known, remaining_args = parser.parse_known_args(args)
    identifier = known.cfg_file_or_class
    try:
        if os.path.exists(identifier):
            config_file = identifier
            config = config_parser.load_config_file(config_file)
            step_class, name = Step._parse_class_and_name(config, config_file=config_file)
        else:
            try:
                step_class = utilities.import_class(
                    utilities.resolve_step_class_alias(identifier), Step
                )
            except (ImportError, AttributeError, TypeError) as err:
                raise ValueError(
                    f"{identifier!r} is not a path to a config file or a Python Step class"
                ) from err
            # Don't validate yet
            config = config_parser.config_from_dict({})
            name = None
            config_file = None
    except Exception as e:
        _print_parser_error(parser, e)
        raise e

    # determine logging parameters
    try:
        if known.verbose:
            log_level = "DEBUG"
        else:
            # allow for "10" as 10 to support numeric log levels
            log_level = (
                int(known.log_level) if known.log_level.isnumeric() else known.log_level
            )

        try:
            log_cfg = _log.load_configuration(log_level, known.log_file, known.log_stream)
        except Exception as e:
            raise ValueError(f"Error parsing logging configuration:\n{e}") from e
    except Exception as e:
        _print_parser_error(parser, e)
        raise e

    log_cfg.set_recording_formatter(step_class._log_records_formatter)

    # set up logging context
    with log_cfg.context(step_class.get_stpipe_loggers()):
        # finish parsing args, make class
        # Determine whether CRDS should be queried for step parameters
        disable_crds_steppars = get_disable_crds_steppars(known.disable_crds_steppars)

        # This creates a config object from the spec file of the step class merged with
        # the spec files of the superclasses of the step class and adds arguments for
        # all of the expected reference files

        # load_spec_file is a method of both Step and Pipeline
        spec = step_class.load_spec_file()

        # It doesn't translate the configspec types -- it instead
        # will accept any string.  However, the types of the arguments will
        # later be verified by configobj itself.
        step_arg_parser = argparse.ArgumentParser(
            description=step_class.__doc__,
        )

        def build_from_spec(subspec, parts=None):
            if parts is None:
                parts = []
            for key, val in subspec.items():
                if isinstance(val, dict):
                    build_from_spec(val, [*parts, key])
                else:
                    comment = subspec.inline_comments.get(key) or ""
                    comment = comment.lstrip("#").strip()
                    # Only show default value if it is not None or the empty string
                    default_value_string = val.split("(")[1].rstrip(")").strip()
                    if default_value_string.lstrip("default=") in ["None", "''", '""']:
                        help_string = comment
                    else:
                        help_string = f"{comment} [{default_value_string}]"
                    argument = "--" + ".".join([*parts, key])
                    if argument[2:] in built_in_configuration_parameters:
                        raise ValueError(
                            "The Step's spec is trying to override a built-in parameter"
                            f" {argument!r}"
                        )
                    step_arg_parser.add_argument(
                        "--" + ".".join([*parts, key]),
                        type=str,
                        help=help_string,
                        metavar="",
                    )

        build_from_spec(spec)

        step_arg_parser.add_argument(
            "args",
            nargs="*",
            help="arguments to pass to step",
        )

        args = step_arg_parser.parse_args(remaining_args)

        positional = args.args
        del args.args

        # This updates config (a ConfigObj) with the values from the command line arguments
        # Config is empty if class specified, otherwise contains values from config file
        # specified on command line
        def set_value(subconf, key, val):
            root, sep, rest = key.partition(".")
            if rest:
                set_value(subconf.setdefault(root, {}), rest, val)
            else:
                val, comment = config._handle_value(val)
                if isinstance(val, str):
                    subconf[root] = FromCommandLine(val)
                else:
                    subconf[root] = val

        for key, val in vars(args).items():
            if val is not None:
                set_value(config, key, val)

        config = step_class.merge_config(config, config_file)

        if len(positional):
            input_file = positional[0]
            if args.input_dir:
                input_file = args.input_dir + "/" + input_file

            # Attempt to retrieve Step parameters from CRDS
            try:
                parameter_cfg = step_class.get_config_from_reference(
                    input_file, disable=disable_crds_steppars
                )
            except (FileNotFoundError, OSError):
                logger.warning("Unable to open input file, cannot get parameters from CRDS")
            else:
                if config:
                    config_parser.merge_config(parameter_cfg, config)
                config = parameter_cfg
        else:
            logger.info("No input file specified, unable to retrieve parameters from CRDS")

        # This is where the step is instantiated
        try:
            step = step_class.from_config_section(
                config,
                name=name,
                config_file=config_file,
            )
        except ValidationError as e:
            # If the configobj validator failed, print usage information.
            _print_important_message("ERROR PARSING CONFIGURATION:", str(e))
            step_arg_parser.print_help()
            raise ValueError(str(e)) from e

        # Define the primary input file.
        # Always have an output_file set on the outermost step
        if len(positional):
            step.set_primary_input(positional[0])
            step.save_results = True

        # Save the step configuration
        if known.save_parameters:
            step.export_config(known.save_parameters, include_metadata=True)
            logger.info(f"Step/Pipeline parameters saved to '{known.save_parameters}'")

        # run step
        try:
            step.run(*positional)
        except Exception as e:
            _print_important_message(
                f"ERROR RUNNING STEP {step_class.__name__!r}:", str(e)
            )

            if known.debug:
                import pdb  # noqa: T100

                pdb.post_mortem()
            else:
                raise

    return step
