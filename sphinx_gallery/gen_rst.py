# -*- coding: utf-8 -*-
# Author: Óscar Nájera
# License: 3-clause BSD
"""
RST file generator
==================

Generate the rst files for the examples by iterating over the python
example files.

Files that generate images should start with 'plot'.
"""
# Don't use unicode_literals here (be explicit with u"..." instead) otherwise
# tricky errors come up with exec(code_blocks, ...) calls
from __future__ import division, print_function, absolute_import
from time import time
import copy
import ast
import codecs
import gc
import os
import re
from shutil import copyfile
import subprocess
import sys
import traceback
import codeop
from io import StringIO
from distutils.version import LooseVersion

from .scrapers import (save_figures, ImagePathIterator, clean_modules,
                       _find_image_ext)
from .utils import replace_py_ipynb, scale_image, get_md5sum, _replace_md5


## ------ monkey-patching plotly.offline.plot -----------------------
import inspect, os
import plotly
plotly_plot = plotly.offline.plot

def patched_plotly_plot(*args, **kwargs):
    stack = inspect.stack()
    filename = stack[1].filename # let's hope this is robust...
    filename_root, _ = os.path.splitext(filename)
    filename_html = filename_root + '.html'
    filename_png = filename_root + '.png'
    figure = plotly.tools.return_figure_from_figure_or_data(*args, True)
    res = plotly_plot(*args, auto_open=False,
		    filename=filename_html)
    plotly.io.write_image(figure, filename_png)
    return res

plotly.offline.plot = patched_plotly_plot


# Try Python 2 first, otherwise load from Python 3
try:
    # textwrap indent only exists in python 3
    from textwrap import indent
except ImportError:
    def indent(text, prefix, predicate=None):
        """Adds 'prefix' to the beginning of selected lines in 'text'.

        If 'predicate' is provided, 'prefix' will only be added to the lines
        where 'predicate(line)' is True. If 'predicate' is not provided,
        it will default to adding 'prefix' to all non-empty lines that do not
        consist solely of whitespace characters.
        """
        if predicate is None:
            def predicate(line):
                return line.strip()

        def prefixed_lines():
            for line in text.splitlines(True):
                yield (prefix + line if predicate(line) else line)
        return ''.join(prefixed_lines())

import sphinx

from . import glr_path_static
from . import sphinx_compatibility
from .backreferences import write_backreferences, _thumbnail_div
from .downloads import CODE_DOWNLOAD
from .py_source_parser import (split_code_and_text_blocks,
                               get_docstring_and_rest)

from .notebook import jupyter_notebook, save_notebook
from .binder import check_binder_conf, gen_binder_rst

try:
    basestring
except NameError:
    basestring = str
    unicode = str

logger = sphinx_compatibility.getLogger('sphinx-gallery')


###############################################################################


class LoggingTee(object):
    """A tee object to redirect streams to the logger"""

    def __init__(self, output_file, logger, src_filename):
        self.output_file = output_file
        self.logger = logger
        self.src_filename = src_filename
        self.first_write = True
        self.logger_buffer = ''

    def write(self, data):
        self.output_file.write(data)

        if self.first_write:
            self.logger.verbose('Output from %s', self.src_filename,
                                color='brown')
            self.first_write = False

        data = self.logger_buffer + data
        lines = data.splitlines()
        if data and data[-1] not in '\r\n':
            # Wait to write last line if it's incomplete. It will write next
            # time or when the LoggingTee is flushed.
            self.logger_buffer = lines[-1]
            lines = lines[:-1]
        else:
            self.logger_buffer = ''

        for line in lines:
            self.logger.verbose('%s', line)

    def flush(self):
        self.output_file.flush()
        if self.logger_buffer:
            self.logger.verbose('%s', self.logger_buffer)
            self.logger_buffer = ''

    # When called from a local terminal seaborn needs it in Python3
    def isatty(self):
        return self.output_file.isatty()


class MixedEncodingStringIO(StringIO):
    """Helper when both ASCII and unicode strings will be written"""

    def write(self, data):
        if not isinstance(data, unicode):
            data = data.decode('utf-8')
        StringIO.write(self, data)


###############################################################################
# The following strings are used when we have several pictures: we use
# an html div tag that our CSS uses to turn the lists into horizontal
# lists.
HLIST_HEADER = """
.. rst-class:: sphx-glr-horizontal

"""

HLIST_IMAGE_TEMPLATE = """
    *

      .. image:: /%s
            :class: sphx-glr-multi-img
"""

SINGLE_IMAGE = """
.. image:: /%s
    :class: sphx-glr-single-img
"""


# This one could contain unicode
CODE_OUTPUT = u""".. rst-class:: sphx-glr-script-out

 Out:

 .. code-block:: none

{0}\n"""

TIMING_CONTENT = """
.. rst-class:: sphx-glr-timing

   **Total running time of the script:** ({0: .0f} minutes {1: .3f} seconds)

"""

SPHX_GLR_SIG = """\n
.. only:: html

 .. rst-class:: sphx-glr-signature

    `Gallery generated by Sphinx-Gallery <https://sphinx-gallery.github.io>`_\n"""  # noqa: E501


def codestr2rst(codestr, lang='python', lineno=None):
    """Return reStructuredText code block from code string"""
    if lineno is not None:
        if LooseVersion(sphinx.__version__) >= '1.3':
            # Sphinx only starts numbering from the first non-empty line.
            blank_lines = codestr.count('\n', 0, -len(codestr.lstrip()))
            lineno = '   :lineno-start: {0}\n'.format(lineno + blank_lines)
        else:
            lineno = '   :linenos:\n'
    else:
        lineno = ''
    code_directive = "\n.. code-block:: {0}\n{1}\n".format(lang, lineno)
    indented_block = indent(codestr, ' ' * 4)
    return code_directive + indented_block


def extract_intro_and_title(filename, docstring):
    """ Extract the first paragraph of module-level docstring. max:95 char"""

    # lstrip is just in case docstring has a '\n\n' at the beginning
    paragraphs = docstring.lstrip().split('\n\n')
    # remove comments and other syntax like `.. _link:`
    paragraphs = [p for p in paragraphs
                  if not p.startswith('.. ') and len(p) > 0]
    if len(paragraphs) == 0:
        raise ValueError(
            "Example docstring should have a header for the example title. "
            "Please check the example file:\n {}\n".format(filename))
    # Title is the first paragraph with any ReSTructuredText title chars
    # removed, i.e. lines that consist of (all the same) 7-bit non-ASCII chars.
    # This conditional is not perfect but should hopefully be good enough.
    title_paragraph = paragraphs[0]
    match = re.search(r'([\w ]+)', title_paragraph)

    if match is None:
        raise ValueError(
            'Could not find a title in first paragraph:\n{}'.format(
                title_paragraph))
    title = match.group(1).strip()
    # Use the title if no other paragraphs are provided
    intro_paragraph = title if len(paragraphs) < 2 else paragraphs[1]
    # Concatenate all lines of the first paragraph and truncate at 95 chars
    intro = re.sub('\n', ' ', intro_paragraph)
    if len(intro) > 95:
        intro = intro[:95] + '...'

    return intro, title


def md5sum_is_current(src_file):
    """Checks whether src_file has the same md5 hash as the one on disk"""

    src_md5 = get_md5sum(src_file)

    src_md5_file = src_file + '.md5'
    if os.path.exists(src_md5_file):
        with open(src_md5_file, 'r') as file_checksum:
            ref_md5 = file_checksum.read()

        return src_md5 == ref_md5

    return False


def save_thumbnail(image_path_template, src_file, file_conf, gallery_conf):
    """Generate and Save the thumbnail image

    Parameters
    ----------
    image_path_template : str
        holds the template where to save and how to name the image
    src_file : str
        path to source python file
    gallery_conf : dict
        Sphinx-Gallery configuration dictionary
    """
    # read specification of the figure to display as thumbnail from main text
    thumbnail_number = file_conf.get('thumbnail_number', 1)
    if not isinstance(thumbnail_number, int):
        raise TypeError(
            'sphinx_gallery_thumbnail_number setting is not a number, '
            'got %r' % (thumbnail_number,))
    thumbnail_image_path, ext = _find_image_ext(image_path_template,
                                                thumbnail_number)

    thumb_dir = os.path.join(os.path.dirname(thumbnail_image_path), 'thumb')
    if not os.path.exists(thumb_dir):
        os.makedirs(thumb_dir)

    base_image_name = os.path.splitext(os.path.basename(src_file))[0]
    thumb_file = os.path.join(thumb_dir,
                              'sphx_glr_%s_thumb.%s' % (base_image_name, ext))

    if src_file in gallery_conf['failing_examples']:
        img = os.path.join(glr_path_static(), 'broken_example.png')
    elif os.path.exists(thumbnail_image_path):
        img = thumbnail_image_path
    elif not os.path.exists(thumb_file):
        # create something to replace the thumbnail
        img = os.path.join(glr_path_static(), 'no_image.png')
        img = gallery_conf.get("default_thumb_file", img)
    else:
        return
    if ext == 'svg':
        copyfile(img, thumb_file)
    else:
        scale_image(img, thumb_file, *gallery_conf["thumbnail_size"])


def generate_dir_rst(src_dir, target_dir, gallery_conf, seen_backrefs):
    """Generate the gallery reStructuredText for an example directory"""

    head_ref = os.path.relpath(target_dir, gallery_conf['src_dir'])
    fhindex = """\n\n.. _sphx_glr_{0}:\n\n""".format(
        head_ref.replace(os.path.sep, '_'))

    with codecs.open(os.path.join(src_dir, 'README.txt'), 'r',
                     encoding='utf-8') as fid:
        fhindex += fid.read()
    # Add empty lines to avoid bug in issue #165
    fhindex += "\n\n"

    if not os.path.exists(target_dir):
        os.makedirs(target_dir)
    # get filenames
    listdir = [fname for fname in os.listdir(src_dir)
               if fname.endswith('.py')]
    # limit which to look at based on regex (similar to filename_pattern)
    listdir = [fname for fname in listdir
               if re.search(gallery_conf['ignore_pattern'],
                            os.path.normpath(os.path.join(src_dir, fname)))
               is None]
    # sort them
    sorted_listdir = sorted(
        listdir, key=gallery_conf['within_subsection_order'](src_dir))
    entries_text = []
    computation_times = []
    build_target_dir = os.path.relpath(target_dir, gallery_conf['src_dir'])
    iterator = sphinx_compatibility.status_iterator(
        sorted_listdir,
        'generating gallery for %s... ' % build_target_dir,
        length=len(sorted_listdir))
    clean_modules(gallery_conf, src_dir)  # fix gh-316
    for fname in iterator:
        intro, time_elapsed = generate_file_rst(
            fname, target_dir, src_dir, gallery_conf)
        clean_modules(gallery_conf, fname)
        src_file = os.path.normpath(os.path.join(src_dir, fname))
        computation_times.append((time_elapsed, src_file))
        this_entry = _thumbnail_div(target_dir, gallery_conf['src_dir'],
                                    fname, intro) + """

.. toctree::
   :hidden:

   /%s\n""" % os.path.join(build_target_dir, fname[:-3]).replace(os.sep, '/')
        entries_text.append(this_entry)

        if gallery_conf['backreferences_dir']:
            write_backreferences(seen_backrefs, gallery_conf,
                                 target_dir, fname, intro)

    for entry_text in entries_text:
        fhindex += entry_text

    # clear at the end of the section
    fhindex += """.. raw:: html\n
    <div style='clear:both'></div>\n\n"""

    return fhindex, computation_times


def handle_exception(exc_info, src_file, script_vars, gallery_conf):
    etype, exc, tb = exc_info
    stack = traceback.extract_tb(tb)
    # Remove our code from traceback:
    if isinstance(exc, SyntaxError):
        # Remove one extra level through ast.parse.
        stack = stack[2:]
    else:
        stack = stack[1:]
    formatted_exception = 'Traceback (most recent call last):\n' + ''.join(
        traceback.format_list(stack) +
        traceback.format_exception_only(etype, exc))

    logger.warning('%s failed to execute correctly: %s', src_file,
                   formatted_exception)

    except_rst = codestr2rst(formatted_exception, lang='pytb')

    # Breaks build on first example error
    if gallery_conf['abort_on_example_error']:
        raise
    # Stores failing file
    gallery_conf['failing_examples'][src_file] = formatted_exception
    script_vars['execute_script'] = False

    return except_rst


class _exec_once(object):
    """Deal with memory_usage calling functions more than once (argh)."""

    def __init__(self, code, globals_):
        self.code = code
        self.globals = globals_
        self.run = False

    def __call__(self):
        if not self.run:
            self.run = True
            exec(self.code, self.globals)


def _memory_usage(func, gallery_conf):
    """Get memory usage of a function call."""
    if gallery_conf['show_memory']:
        from memory_profiler import memory_usage
        assert callable(func)
        mem, out = memory_usage(func, max_usage=True, retval=True,
                                multiprocess=True)
        mem = mem[0]
    else:
        out = func()
        mem = 0
    return out, mem


def _get_memory_base(gallery_conf):
    """Get the base amount of memory used by running a Python process."""
    if not gallery_conf['show_memory']:
        memory_base = 0
    else:
        # There might be a cleaner way to do this at some point
        from memory_profiler import memory_usage
        sleep, timeout = (1, 2) if sys.platform == 'win32' else (0.5, 1)
        proc = subprocess.Popen(
            [sys.executable, '-c',
             'import time, sys; time.sleep(%s); sys.exit(0)' % sleep],
            close_fds=True)
        memories = memory_usage(proc, interval=1e-3, timeout=timeout)
        kwargs = dict(timeout=timeout) if sys.version_info >= (3, 5) else {}
        proc.communicate(**kwargs)
        # On OSX sometimes the last entry can be None
        memories = [mem for mem in memories if mem is not None] + [0.]
        memory_base = max(memories)
    return memory_base


def execute_code_block(compiler, block, example_globals,
                       script_vars, gallery_conf):
    """Executes the code block of the example file"""
    blabel, bcontent, lineno = block
    # If example is not suitable to run, skip executing its blocks
    if not script_vars['execute_script'] or blabel == 'text':
        script_vars['memory_delta'].append(0)
        return ''

    cwd = os.getcwd()
    # Redirect output to stdout and
    orig_stdout = sys.stdout
    src_file = script_vars['src_file']

    # First cd in the original example dir, so that any file
    # created by the example get created in this directory

    my_stdout = MixedEncodingStringIO()
    os.chdir(os.path.dirname(src_file))

    sys_path = copy.deepcopy(sys.path)
    sys.path.append(os.getcwd())
    sys.stdout = LoggingTee(my_stdout, logger, src_file)

    try:
        dont_inherit = 1
        code_ast = compile(bcontent, src_file, 'exec',
                           ast.PyCF_ONLY_AST | compiler.flags, dont_inherit)
        ast.increment_lineno(code_ast, lineno - 1)
        # don't use unicode_literals at the top of this file or you get
        # nasty errors here on Py2.7
        _, mem = _memory_usage(_exec_once(
            compiler(code_ast, src_file, 'exec'), example_globals),
            gallery_conf)
    except Exception:
        sys.stdout.flush()
        sys.stdout = orig_stdout
        except_rst = handle_exception(sys.exc_info(), src_file, script_vars,
                                      gallery_conf)
        # python2.7: Code was read in bytes needs decoding to utf-8
        # unless future unicode_literals is imported in source which
        # make ast output unicode strings
        if hasattr(except_rst, 'decode') and not \
                isinstance(except_rst, unicode):
            except_rst = except_rst.decode('utf-8')

        code_output = u"\n{0}\n\n\n\n".format(except_rst)
        # still call this even though we won't use the images so that
        # figures are closed
        save_figures(block, script_vars, gallery_conf)
        mem = 0
    else:
        sys.stdout.flush()
        sys.stdout = orig_stdout
        sys.path = sys_path
        os.chdir(cwd)

        my_stdout = my_stdout.getvalue().strip().expandtabs()
        if my_stdout:
            stdout = CODE_OUTPUT.format(indent(my_stdout, u' ' * 4))
        else:
            stdout = ''
        images_rst = save_figures(block, script_vars, gallery_conf)
        code_output = u"\n{0}\n\n{1}\n\n".format(images_rst, stdout)

    finally:
        os.chdir(cwd)
        sys.path = sys_path
        sys.stdout = orig_stdout
    script_vars['memory_delta'].append(mem)

    return code_output


def executable_script(src_file, gallery_conf):
    """Validate if script has to be run according to gallery configuration

    Parameters
    ----------
    src_file : str
        path to python script

    gallery_conf : dict
        Contains the configuration of Sphinx-Gallery

    Returns
    -------
    bool
        True if script has to be executed
    """

    filename_pattern = gallery_conf.get('filename_pattern')
    execute = re.search(filename_pattern, src_file) and gallery_conf[
        'plot_gallery']
    return execute


def execute_script(script_blocks, script_vars, gallery_conf):
    """Execute and capture output from python script already in block structure

    Parameters
    ----------
    script_blocks : list
        (label, content, line_number)
        List where each element is a tuple with the label ('text' or 'code'),
        the corresponding content string of block and the leading line number
    script_vars : dict
        Configuration and run time variables
    gallery_conf : dict
        Contains the configuration of Sphinx-Gallery

    Returns
    -------
    output_blocks : list
        List of strings where each element is the restructured text
        representation of the output of each block
    time_elapsed : float
        Time elapsed during execution
    """

    example_globals = {
        # A lot of examples contains 'print(__doc__)' for example in
        # scikit-learn so that running the example prints some useful
        # information. Because the docstring has been separated from
        # the code blocks in sphinx-gallery, __doc__ is actually
        # __builtin__.__doc__ in the execution context and we do not
        # want to print it
        '__doc__': '',
        # Examples may contain if __name__ == '__main__' guards
        # for in example scikit-learn if the example uses multiprocessing
        '__name__': '__main__',
        # Don't ever support __file__: Issues #166 #212
    }

    argv_orig = sys.argv[:]
    if script_vars['execute_script']:
        # We want to run the example without arguments. See
        # https://github.com/sphinx-gallery/sphinx-gallery/pull/252
        # for more details.
        sys.argv[0] = script_vars['src_file']
        sys.argv[1:] = []

    t_start = time()
    gc.collect()
    _, memory_start = _memory_usage(lambda: None, gallery_conf)
    compiler = codeop.Compile()
    # include at least one entry to avoid max() ever failing
    script_vars['memory_delta'] = [memory_start]
    output_blocks = [execute_code_block(compiler, block,
                                        example_globals,
                                        script_vars, gallery_conf)
                     for block in script_blocks]
    time_elapsed = time() - t_start
    script_vars['memory_delta'] = (  # actually turn it into a delta now
        max(script_vars['memory_delta']) - memory_start)

    sys.argv = argv_orig

    # Write md5 checksum if the example was meant to run (no-plot
    # shall not cache md5sum) and has built correctly
    if script_vars['execute_script']:
        with open(script_vars['target_file'] + '.md5', 'w') as file_checksum:
            file_checksum.write(get_md5sum(script_vars['target_file']))
        gallery_conf['passing_examples'].append(script_vars['src_file'])

    return output_blocks, time_elapsed


def generate_file_rst(fname, target_dir, src_dir, gallery_conf):
    """Generate the rst file for a given example.

    Parameters
    ----------
    fname : str
        Filename of python script
    target_dir : str
        Absolute path to directory in documentation where examples are saved
    src_dir : str
        Absolute path to directory where source examples are stored
    gallery_conf : dict
        Contains the configuration of Sphinx-Gallery

    Returns
    -------
    intro: str
        The introduction of the example
    time_elapsed : float
        seconds required to run the script
    """
    src_file = os.path.normpath(os.path.join(src_dir, fname))
    target_file = os.path.join(target_dir, fname)
    _replace_md5(src_file, target_file, 'copy')

    intro, _ = extract_intro_and_title(fname,
                                       get_docstring_and_rest(src_file)[0])

    executable = executable_script(src_file, gallery_conf)

    if md5sum_is_current(target_file):
        if executable:
            gallery_conf['stale_examples'].append(target_file)
        return intro, 0

    image_dir = os.path.join(target_dir, 'images')
    if not os.path.exists(image_dir):
        os.makedirs(image_dir)

    base_image_name = os.path.splitext(fname)[0]
    image_fname = 'sphx_glr_' + base_image_name + '_{0:03}.png'
    image_path_template = os.path.join(image_dir, image_fname)

    script_vars = {
        'execute_script': executable,
        'image_path_iterator': ImagePathIterator(image_path_template),
        'src_file': src_file,
        'target_file': target_file}

    file_conf, script_blocks = split_code_and_text_blocks(src_file)
    output_blocks, time_elapsed = execute_script(script_blocks,
                                                 script_vars,
                                                 gallery_conf)

    logger.debug("%s ran in : %.2g seconds\n", src_file, time_elapsed)

    example_rst = rst_blocks(script_blocks, output_blocks,
                             file_conf, gallery_conf)
    memory_used = gallery_conf['memory_base'] + script_vars['memory_delta']
    if not executable:
        time_elapsed = memory_used = 0.  # don't let the output change
    save_rst_example(example_rst, target_file, time_elapsed, memory_used,
                     gallery_conf)

    save_thumbnail(image_path_template, src_file, file_conf, gallery_conf)

    example_nb = jupyter_notebook(script_blocks, gallery_conf)
    ipy_fname = replace_py_ipynb(target_file) + '.new'
    save_notebook(example_nb, ipy_fname)
    _replace_md5(ipy_fname)

    return intro, time_elapsed


def rst_blocks(script_blocks, output_blocks, file_conf, gallery_conf):
    """Generates the rst string containing the script prose, code and output

    Parameters
    ----------
    script_blocks : list
        (label, content, line_number)
        List where each element is a tuple with the label ('text' or 'code'),
        the corresponding content string of block and the leading line number
    output_blocks : list
        List of strings where each element is the restructured text
        representation of the output of each block
    file_conf : dict
        File-specific settings given in source file comments as:
        ``# sphinx_gallery_<name> = <value>``
    gallery_conf : dict
        Contains the configuration of Sphinx-Gallery

    Returns
    -------
    out : str
        rst notebook
    """

    # A simple example has two blocks: one for the
    # example introduction/explanation and one for the code
    is_example_notebook_like = len(script_blocks) > 2
    example_rst = u""  # there can be unicode content
    for (blabel, bcontent, lineno), code_output in \
            zip(script_blocks, output_blocks):
        if blabel == 'code':

            if not file_conf.get('line_numbers',
                                 gallery_conf.get('line_numbers', False)):
                lineno = None

            code_rst = codestr2rst(bcontent, lang=gallery_conf['lang'],
                                   lineno=lineno) + '\n'
            if is_example_notebook_like:
                example_rst += code_rst
                example_rst += code_output
            else:
                example_rst += code_output
                if 'sphx-glr-script-out' in code_output:
                    # Add some vertical space after output
                    example_rst += "\n\n|\n\n"
                example_rst += code_rst
        else:
            block_separator = '\n\n' if not bcontent.endswith('\n') else '\n'
            example_rst += bcontent + block_separator
    return example_rst


def save_rst_example(example_rst, example_file, time_elapsed,
                     memory_used, gallery_conf):
    """Saves the rst notebook to example_file including header & footer

    Parameters
    ----------
    example_rst : str
        rst containing the executed file content
    example_file : str
        Filename with full path of python example file in documentation folder
    time_elapsed : float
        Time elapsed in seconds while executing file
    memory_used : float
        Additional memory used during the run.
    gallery_conf : dict
        Sphinx-Gallery configuration dictionary
    """

    ref_fname = os.path.relpath(example_file, gallery_conf['src_dir'])
    ref_fname = ref_fname.replace(os.path.sep, "_")

    binder_conf = check_binder_conf(gallery_conf.get('binder'))

    binder_text = (" or run this example in your browser via Binder"
                   if len(binder_conf) else "")
    example_rst = (".. note::\n"
                   "    :class: sphx-glr-download-link-note\n\n"
                   "    Click :ref:`here <sphx_glr_download_{0}>` "
                   "to download the full example code{1}\n"
                   ".. rst-class:: sphx-glr-example-title\n\n"
                   ".. _sphx_glr_{0}:\n\n"
                   ).format(ref_fname, binder_text) + example_rst

    if time_elapsed >= gallery_conf["min_reported_time"]:
        time_m, time_s = divmod(time_elapsed, 60)
        example_rst += TIMING_CONTENT.format(time_m, time_s)
    if gallery_conf['show_memory']:
        example_rst += ("**Estimated memory usage:** {0: .0f} MB\n\n"
                        .format(memory_used))

    # Generate a binder URL if specified
    binder_badge_rst = ''
    if len(binder_conf) > 0:
        binder_badge_rst += gen_binder_rst(example_file, binder_conf,
                                           gallery_conf)

    fname = os.path.basename(example_file)
    example_rst += CODE_DOWNLOAD.format(fname,
                                        replace_py_ipynb(fname),
                                        binder_badge_rst,
                                        ref_fname)
    example_rst += SPHX_GLR_SIG

    write_file_new = re.sub(r'\.py$', '.rst.new', example_file)
    with codecs.open(write_file_new, 'w', encoding="utf-8") as f:
        f.write(example_rst)
    # in case it wasn't in our pattern, only replace the file if it's
    # still stale.
    _replace_md5(write_file_new)
