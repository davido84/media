import argparse
import logging

import dup
import music_titles
from music_titles import fix_titles
from dup import rm_dup
import media_util

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(levelname)-8s %(message)s',
                    datefmt='%m-%d %H:%M',
                    filename='d:/cm.log',
                    filemode='w')

# define a Handler which writes INFO messages or higher to the sys.stderr
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)

# set a format which is simpler for console use
# formatter = logging.Formatter('%(name)-12s: %(levelname)-8s %(message)s')
formatter = logging.Formatter('%(levelname)-8s %(message)s')
# tell the handler to use this format
console.setFormatter(formatter)
# add the handler to the root logger
logging.getLogger().addHandler(console)

# Now, define a couple of other loggers which might represent areas in your
# application:

# logger1 = logging.getLogger('myapp.area1')
# logger2 = logging.getLogger('myapp.area2')
#
# logger1.debug('Quick zephyrs blow, vexing daft Jim.')
# logger1.info('How quickly daft jumping zebras vex.')
# logger2.warning('Jail zesty vixen who grabbed pay from quack.')
# logger2.error('The five boxing wizards jump quickly.')


def main():
    parser = argparse.ArgumentParser(description='Convert Music Files')
    parser.add_argument('-i', '--input', type=str, help='Input folder or file', default='e:/music')
    parser.add_argument('-d', '--dry-run', action='store_true', default=False, help='Dry run')
    subparsers = parser.add_subparsers(title='Commands', description='Convert music commands')

    parser_fix_titles = subparsers.add_parser('fix-titles', help='Fix music filenames', aliases=['ft'])
    parser_fix_titles.set_defaults(func=fix_titles)

    parser_remove_duplicates = subparsers.add_parser('rm-dup', help='Remove duplicates', aliases=['rd'])
    parser_remove_duplicates.set_defaults(func=rm_dup)

    parser_remove_duplicates = subparsers.add_parser('dup', help='Show duplicate files.')
    parser_remove_duplicates.set_defaults(func=dup.show_duplicates, desc='Show duplicates.')

    parser_validate_metadata = subparsers.add_parser('validate-meta', help='Show duplicate files.', aliases=['vm'])
    parser_validate_metadata.set_defaults(func=music_titles.validate_metadata, desc='Validate metadata.')


    args = parser.parse_args()
    with media_util.media_method(args.desc):
        args.func(args)


if __name__ == "__main__":
    main()