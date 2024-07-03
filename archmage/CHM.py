# -*- coding: utf-8 -*-
#
# archmage -- CHM decompressor
# Copyright (c) 2003 Eugeny Korekin <aaaz@users.sourceforge.net>
# Copyright (c) 2005-2009 Basil Shubin <bashu@users.sourceforge.net>
# Copyright (c) 2015-2020 Misha Gusarov <dottedmag@dottedmag.net>
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51 Franklin
# Street, Fifth Floor, Boston, MA 02110-1301, USA.
#

import os
import sys
import re
import shutil
import errno
import string
import tempfile
import os.path
from enum import Enum
from typing import List, Union

import archmage

from archmage.CHMParser import SitemapFile, PageLister, ImageCatcher, TOCCounter

# import PyCHM bindings
try:
    from chm import chmlib  # type: ignore
except ImportError as msg:
    sys.exit(
        "ImportError: %s\nPlease check README file for system requirements."
        % msg
    )

# External file converters
from archmage.chmtotext import chmtotext
from archmage.htmldoc import htmldoc


class Action(Enum):
    """
    Enumerates possible actions to perform on CHM files, including extracting
    content, dumping HTML, converting CHM to text or HTML, and converting CHM to
    PDF.

    Attributes:
        EXTRACT (Enum): 1.
        DUMPHTML (INT): 2 in value, representing the action of dumping HTML content.
        CHM2TXT (Integer): 3, representing the conversion of CHM files to text format.
        CHM2HTML (Integer): 4 in value, representing the action of converting CHM
            files to HTML format.
        CHM2PDF (int): 5th in the list of available actions, representing the
            conversion of CHM files to PDF format.

    """
    EXTRACT = 1
    DUMPHTML = 2
    CHM2TXT = 3
    CHM2HTML = 4
    CHM2PDF = 5


PARENT_RE = re.compile(r"(^|/|\\)\.\.(/|\\|$)")


class FileSource:
    """
    Provides methods for listing and retrieving files from a CHM file, as well as
    closing the file handle.

    Attributes:
        _chm (chmlibChm): Used to manage a CHM (Compact HTML) file.

    """
    def __init__(self, filename):
        self._chm = chmlib.chm_open(filename)

    def listdir(self):
        """
        Within the `FileSource` class enumerates and stores the file paths in a
        list when passed a valid input path.

        Returns:
            Liststr: A list of strings containing the file paths from the directory
            being scanned.

        """
        def get_name(chmfile, ui, out):
            """
            Takes a ChM file, user input, and output parameters, and appends the
            path of the user-provided file to the output list if it is not the
            root directory.

            Args:
                chmfile (object): Used to represent a file path for the CHM file
                    being processed.
                ui (chmfilePath): Used to store the decoded path of a file.
                out (stdvectorstdstring): Used to store the path of the file.

            Returns:
                chmlibCHM_ENUMERATOR_CONTINUE: A enumerated value that indicates
                whether the function should continue iterating over the file names
                or stop.

            """
            path = ui.path.decode("utf-8")
            if path != "/":
                out.append(path)
            return chmlib.CHM_ENUMERATOR_CONTINUE

        out: List[str] = []
        if (
            chmlib.chm_enumerate(
                self._chm, chmlib.CHM_ENUMERATE_ALL, get_name, out
            )
            == 0
        ):
            sys.exit("UnknownError: CHMLIB or PyCHM bug?")
        return out

    def get(self, name):
        """
        Resolves an object in the Chm library, retrieves its contents, and returns
        them as a string.

        Args:
            name (str): Used to specify the object to resolve.

        Returns:
            object: A `content`.

        """
        result, ui = chmlib.chm_resolve_object(self._chm, name.encode("utf-8"))
        if result != chmlib.CHM_RESOLVE_SUCCESS:
            return None
        size, content = chmlib.chm_retrieve_object(self._chm, ui, 0, ui.length)
        if size == 0:
            return None
        return content

    def close(self):
        chmlib.chm_close(self._chm)


class DirSource:
    """
    Provides methods for listing and reading files in a directory, along with a
    method to close the source.

    Attributes:
        dirname (str): A string representing the root directory for listing files.

    """
    def __init__(self, dirname):
        self.dirname = dirname
    def listdir(self):
        """
        Within a class `DirSource` recursively traverses subdirectories and appends
        file paths to a list.

        Returns:
            list: A container object that stores a sequence of entry points.

        """
        entries = []
        for dir, _, files in os.walk(self.dirname):
            for f in files:
                entries.append(
                    "/" + os.path.relpath(os.path.join(dir, f), self.dirname)
                )
        return entries
    def get(self, filename):
        """
        Retrieves a file from a directory by opening it in binary mode and returning
        its contents if successful, or `None` if an error occurs.

        Args:
            filename (str): Used to specify the name of the file to be read from
                the directory.

        Returns:
            Optionalbytes: A representation of the contents of a file.

        """
        with open(self.dirname + filename, "rb") as fh:
            if fh is None:
                return None
            return fh.read()
    def close(self):
        pass


class CHM:
    """
    Extracts CHM content from a given source directory and generates HTML, PDF or
    CHM files based on user input. It also provides methods for processing templates,
    extracting entries, and dumping HTML contents.

    Attributes:
        cache (dict): Used to store the results of previous calls to methods such
            as `deftopic`, `frontpage`, `templates`, and `get_template`. It allows
            for faster execution of these methods by avoiding redundant computation.
        source (instanceobject): Used to store the source file path or URL from
            which the CHM content is being extracted.
        sourcename (str): Used to specify the source name for chm2text conversion.
        __dict__ (instance): Used to store the attributes and methods of the class
            as a dictionary, allowing them to be accessed directly without having
            to use the class name and dot notation.
        aux_re (regular): Used to match any malicious or unwanted names that may
            appear in the CHM files, such as parent references or malicious content.
        auxes (instance): A list of regular expressions that match malicious or
            unwanted content, such as HTML tags or URLs.
        topicstree (TOCCounter): Used to store the hierarchy of topics in a CHM
            file. It represents the number of top-level categories (or "tocs") in
            the file, and can be used to determine the maximum level of indentation
            for nested topics.
        topics (instanceclass): A list of topic names that are contained within
            the CHM file, indicating the hierarchy of topics in the file.
        contents (str): Used to store the HTML contents of a CHM file, which can
            be extracted using the `extract_entry()` method.

    """
    def __init__(self, name):
        """
        Of the `CHM` class initializes object attributes and performs actions on
        them. It creates a dictionary for caching, loads sources from files or
        directories using DirSource or FileSource classes, and defines auxilary
        regular expressions. Finally, it sets up a topics tree and contents using
        the `topics()` method.

        Args:
            name (str): Used to specify the path to the configuration file.

        """
        self.cache = {}
        # Name of source directory with CHM content
        if os.path.isdir(name):
            self.source: Union[DirSource, FileSource] = DirSource(name)
        else:
            self.source = FileSource(name)
        self.sourcename = name
        # Import variables from config file into namespace
        exec(
            compile(
                open(archmage.config, "rb").read(), archmage.config, "exec"
            ),
            self.__dict__,
        )

        # build regexp from the list of auxiliary files
        self.aux_re = "|".join([re.escape(s) for s in self.auxes])

        # Get and parse 'Table of Contents'
        try:
            self.topicstree = self.topics()
        except AttributeError:
            self.topicstree = None
        self.contents = SitemapFile(self.topicstree).parse()

    def close(self):
        self.source.close()

    def entries(self):
        """
        Retrieves and caches a list of entries from a subfunction `_entries()`
        within the `CHM` class, returning the cached value.

        Returns:
            list: A cache of entries.

        """
        if "entries" not in self.cache:
            self.cache["entries"] = self._entries()
        return self.cache["entries"]

    def _entries(self):
        return self.source.listdir()

    # retrieves the list of HTML files contained into the CHM file, **in order**
    # (that's the important bit).
    # (actually performed by the PageLister class)
    def html_files(self):
        """
        In the `CHM` class retrieves and caches a list of HTML files. If the cache
        does not exist, it calls the `_html_files` method to generate the list and
        stores it in the cache for future use.

        Returns:
            list: A cache of the method's internal _html_files() call.

        """
        if "html_files" not in self.cache:
            self.cache["html_files"] = self._html_files()
        return self.cache["html_files"]

    def _html_files(self):
        """
        Within the CHM class takes its instance's topicstree as input and returns
        a list of pages generated from it using PageLister().

        """
        lister = PageLister()
        lister.feed(self.topicstree)
        return lister.pages

    # retrieves the list of images urls contained into the CHM file.
    # (actually performed by the ImageCatcher class)
    def image_urls(self):
        """
        Retrieves and stores image URLs in a cache for later use by the `CHM` class.

        Returns:
            list: A cache of image URLs.

        """
        if "image_urls" not in self.cache:
            self.cache["image_urls"] = self._image_urls()
        return self.cache["image_urls"]

    def _image_urls(self):
        """
        Within the CHM class takes an input of self.html_files() and generates a
        list of image URLs by using ImageCatcher to feed each file and then appending
        the image URL to a list if it is not already present.

        """
        out: List[str] = []
        image_catcher = ImageCatcher()
        for file in self.html_files():
            # Use latin-1, as it will accept any byte sequences
            image_catcher.feed(
                Entry(
                    self.source, file, self.filename_case, self.restore_framing
                ).correct().decode("latin-1")
            )
            for image_url in image_catcher.imgurls:
                if not out.count(image_url):
                    out.append(image_url)
        return out

    # retrieves a dictionary of actual file entries and corresponding urls into
    # the CHM file
    def image_files(self):
        """
        In the `CHM` class retrieves or caches an image file list.

        Returns:
            list: The result of calling the private method `_image_files`.

        """
        if "image_files" not in self.cache:
            self.cache["image_files"] = self._image_files()
        return self.cache["image_files"]

    def _image_files(self):
        """
        For a `CHM` class iterates through image URLs and entries, updating an
        output dictionary with image URLs and their corresponding entries if the
        URL matches the entry and hasn't been already recorded.

        """
        out = {}
        for image_url in self.image_urls():
            for entry in self.entries():
                if (
                    re.search(image_url, entry.lower())
                    and entry.lower() not in out
                ):
                    out.update({entry: image_url})
        return out

    # Get topics file
    def topics(self):
        """
        Retrieves and caches a list of topics from the `_topics()` method, which
        is not shown in the snippet. The cached list is returned when called again.

        Returns:
            dict: A cache of the topics data.

        """
        if "topics" not in self.cache:
            self.cache["topics"] = self._topics()
        return self.cache["topics"]

    def _topics(self):
        """
        Within a class named CHM, searches for entries with a `.hhc` extension and
        returns an Entry object with additional parameters.

        """
        for e in self.entries():
            if e.lower().endswith(".hhc"):
                return Entry(
                    self.source,
                    e,
                    self.filename_case,
                    self.restore_framing,
                    frontpage=self.frontpage(),
                ).get()

    # use first page as deftopic. Note: without heading slash
    def deftopic(self):
        """
        In the `CHM` class checks if the "deftopic" key is absent in the cache,
        and if not, it retrieves the value from the `_deftopic` method and stores
        it in the cache.

        Returns:
            object: Stored in a cache.

        """
        if "deftopic" not in self.cache:
            self.cache["deftopic"] = self._deftopic()
        return self.cache["deftopic"]

    def _deftopic(self):
        """
        Determines and returns the topic of an HTML file based on its name, replacing
        any leading `/` characters and lowercasing the result.

        """
        if self.html_files()[0].startswith("/"):
            return self.html_files()[0].replace("/", "", 1).lower()
        return self.html_files()[0].lower()

    # Get frontpage name
    def frontpage(self):
        """
        Retrieves and caches the front page of a web application if it hasn't been
        cached before, otherwise it returns the cached value.

        Returns:
            object: Stored in the cache if it has not been previously set.

        """
        if "frontpage" not in self.cache:
            self.cache["frontpage"] = self._frontpage()
        return self.cache["frontpage"]

    def _frontpage(self):
        """
        Determines the front page of a website by iterating over a list of filenames,
        checking if each one is the front page, and if so, renaming it with a
        numbered version of the name.

        """
        frontpage = os.path.join("/", "index.html")
        index = 2  # index2.html and etc.
        for filename in self.entries():
            if frontpage == filename:
                frontpage = os.path.join("/", ("index%s.html" % index))
                index += 1
        return frontpage

    # Get all templates files
    def templates(self):
        """
        Retrieves a cache of pre-defined templates for an instance of the `CHM`
        class, or creates and stores a new set of templates if none exist in the
        cache.

        Returns:
            dict: A cache of templates for the current user.

        """
        if "templates" not in self.cache:
            self.cache["templates"] = self._templates()
        return self.cache["templates"]

    def _templates(self):
        """
        In the CHM class lists all files in the templates directory and returns a
        list of file paths that are not already stored as entries in the class's
        `entries` attribute.

        """
        out = []
        for file in os.listdir(self.templates_dir):
            if os.path.isfile(os.path.join(self.templates_dir, file)):
                if os.path.join("/", file) not in self.entries():
                    out.append(os.path.join("/", file))
        return out

    # Get ToC levels
    def toclevels(self):
        """
        In the `CHM` class retrieves and stores the toclevels of an object in its
        cache, if not already present.

        Returns:
            dict: A cache of toclevels data obtained through the `_toclevels`
            method call.

        """
        if "toclevels" not in self.cache:
            self.cache["toclevels"] = self._toclevels()
        return self.cache["toclevels"]

    def _toclevels(self):
        """
        Within the CHM class takes a given string and counts the levels of indentation
        in it, returning the maximum level detected or the actual count if it's
        lower than the maximum level set by the `maxtoclvl` attribute.

        """
        counter = TOCCounter()
        # Use latin-1, as it will accept any byte sequences
        counter.feed(self.topicstree.decode("latin-1"))
        if counter.count > self.maxtoclvl:
            return self.maxtoclvl
        else:
            return counter.count

    def get_template(self, name):
        """
        Retrieves an HTML template from the specified location and substitutes
        placeholder values with the class's properties: title, contents, deftopic,
        bcolor, and fcolor.

        Args:
            name (str): Passed in as an argument, which represents the name of the
                template to be retrieved.

        Returns:
            stringTemplate: A modified version of a template file based on user
            input and predefined variables.

        """
        if name == self.frontpage():
            tpl = open(os.path.join(self.templates_dir, "index.html")).read()
        else:
            tpl = open(
                os.path.join(self.templates_dir, os.path.basename(name))
            ).read()
        params = {
            "title": self.title,
            "contents": self.contents,
            "deftopic": self.deftopic(),
            "bcolor": self.bcolor,
            "fcolor": self.fcolor,
        }
        return string.Template(tpl).substitute(params)

    def process_templates(self, destdir="."):
        """
        Of the CHM class writes the templates in the `self.templates()` list to
        the specified destination directory, creating new files if necessary. It
        also copies the icons from the `self.icons_dir` to the destination directory
        if it does not already exist.

        Args:
            destdir (str): Used to specify the directory where the templates will
                be written. It has an default value of ".".

        """
        for template in self.templates():
            open(os.path.join(destdir, os.path.basename(template)), "w").write(
                self.get_template(template)
            )
        if self.frontpage() not in self.templates():
            open(
                os.path.join(destdir, os.path.basename(self.frontpage())), "w"
            ).write(self.get_template("index.html"))
        if not os.path.exists(os.path.join(destdir, "icons/")):
            shutil.copytree(
                os.path.join(self.icons_dir), os.path.join(destdir, "icons/")
            )

    def extract_entry(self, entry, output_file, destdir=".", correct=False):
        # process output entry, remove first '/' in entry name
        """
        Within the `CHM` class extracts an entry from a source file and writes it
        to a specified output file, creating directories as necessary for the
        entry's file name.

        Args:
            entry (Entry): Used to represent an entry in a file or directory.
            output_file (osPathlike): Used to specify the output file path where
                the extracted entry will be saved.
            destdir (str): Used to specify the directory where the extracted entry
                will be saved. It has an default value of ".", which means the
                current working directory.
            correct (bool): Used to indicate whether the entry should be corrected
                or not during extraction.

        """
        fname = output_file.lower().replace("/", "", 1)
        # get directory name for file fname if any
        dname = os.path.dirname(os.path.join(destdir, fname))
        # if dname is a directory and it's not exist, than create it
        if dname and not os.path.exists(dname):
            os.makedirs(dname)
        # otherwise write a file from CHM entry
        if not os.path.isdir(os.path.join(destdir, fname)):
            # write CHM entry content into the file, corrected or as is
            if correct:
                open(os.path.join(destdir, fname), "wb").write(
                    Entry(
                        self.source,
                        entry,
                        self.filename_case,
                        self.restore_framing,
                    ).correct()
                )
            else:
                open(os.path.join(destdir, fname), "wb").write(
                    Entry(
                        self.source,
                        entry,
                        self.filename_case,
                        self.restore_framing,
                    ).get()
                )

    def extract_entries(self, entries=[], destdir=".", correct=False):
        """
        In the `CHM` class takes an optional list of entries, checks if each entry
        matches the auxilary regular expression pattern, and extracts the entry
        if it matches. If the entry is a parent directory, it raises a RuntimeError.
        Otherwise, it extracts the entry to the specified destination directory
        and sets the correct flag to `True`.

        Args:
            entries (list): Represented as `[e for e in entries]`. It refers to
                the list of strings that contain file names or paths to be extracted.
            destdir (str): Used to specify the destination directory for extracted
                entries.
            correct (bool): Used to indicate whether the entry should be extracted
                correctly or not.

        """
        for e in entries:
            # if entry is auxiliary file, than skip it
            if re.match(self.aux_re, e):
                continue
            if PARENT_RE.search(e):
                raise RuntimeError("Giving up on malicious name: %s" % e)
            self.extract_entry(
                e, output_file=e, destdir=destdir, correct=correct
            )

    def extract(self, destdir):
        """
        Of the `CHM` class creates a directory if it does not exist, then extracts
        entries and templates into that directory using `os.mkdir` and `os.copyfile`.
        If the destination directory already exists, the function exits with an
        error message.

        Args:
            destdir (str): Used to specify the destination directory for extraction.

        """
        try:
            # Create destination directory
            os.mkdir(destdir)
            # make raw content extraction
            self.extract_entries(entries=self.entries(), destdir=destdir)
            # process templates
            self.process_templates(destdir=destdir)
        except OSError as error:
            if error.errno == errno.EEXIST:
                sys.exit("%s is already exists" % destdir)

    def dump_html(self, output=sys.stdout):
        """
        Iterates over a list of HTML files and prints out entry information for
        each file that matches a specified regular expression.

        Args:
            output (object): Used to print the output of the function.

        """
        for e in self.html_files():
            # if entry is auxiliary file, than skip it
            if re.match(self.aux_re, e):
                continue
            print(
                Entry(
                    self.source, e, self.filename_case, self.restore_framing
                ).get(),
                file=output,
            )

    def chm2text(self, output=sys.stdout):
        """
        Takes an input file and produces a text representation of its content using
        the CHM tool.

        Args:
            output (object): Used to write the generated text to the output stream.

        """
        for e in self.html_files():
            # if entry is auxiliary file, than skip it
            if re.match(self.aux_re, e):
                continue
            # to use this function you should have 'lynx' or 'elinks' installed
            chmtotext(
                input=Entry(
                    self.source, e, self.filename_case, self.restore_framing
                ).get(),
                cmd=self.chmtotext,
                output=output,
            )

    def htmldoc(self, output, format=Action.CHM2HTML):
        """
        Extracts CHM content into a temporary directory, processes the content
        using the specified format (CHM2HTML or CHM2PDF), and generates an output
        file based on the input files and options provided.

        Args:
            output (str): Used to specify the output file path for the generated
                HTML document, with the format option determining whether the
                output is in CHM2HTML or CHM2PDF format.
            format (str): Used to specify the output format of the HTML documentation,
                which can be either CHM2HTML or CHM2PDF.

        """
        # Extract CHM content into temporary directory
        output = output.replace(" ", "_")
        tempdir = tempfile.mkdtemp(prefix=output.rsplit(".", 1)[0])
        self.extract_entries(
            entries=self.html_files(), destdir=tempdir, correct=True
        )
        # List of temporary files
        files = [
            os.path.abspath(tempdir + file.lower())
            for file in self.html_files()
        ]
        if format == Action.CHM2HTML:
            options = self.chmtohtml
            # change output from single html file to a directory with html file
            # and images
            if self.image_files():
                dirname = archmage.file2dir(output)
                if os.path.exists(dirname):
                    sys.exit("%s is already exists" % dirname)
                # Extract image files
                os.mkdir(dirname)
                # Extract all images
                for key, value in list(self.image_files().items()):
                    self.extract_entry(
                        entry=key, output_file=value, destdir=dirname
                    )
                # Fix output file name
                output = os.path.join(dirname, output)
        elif format == Action.CHM2PDF:
            options = self.chmtopdf
            if self.image_files():
                # Extract all images
                for key, value in list(self.image_files().items()):
                    self.extract_entry(
                        entry=key, output_file=key.lower(), destdir=tempdir
                    )
        htmldoc(files, self.htmldoc_exec, options, self.toclevels(), output)
        # Remove temporary files
        shutil.rmtree(path=tempdir)


class Entry(object):
    """
    Manages entry content, performing actions such as reading, lowercasing links,
    and adding restore framing JavaScript code.

    Attributes:
        source (object): Used to store the source content of the entry.
        name (str): Used to store the name of the entry.
        filename_case (str): Used to modify the filename case of the entry content
            during the correction process.
        restore_framing (bool): Used to indicate whether framing links should be
            restored when the entry's content is read.
        frontpage (ospathbasename): Used to specify the name of a file with which
            the framing will be restored.

    """

    def __init__(
        self,
        source,
        name,
        filename_case,
        restore_framing,
        frontpage="index.html",
    ):
        # Entry source
        """
        Initializes an `Entry` object by setting instance variables from arguments
        passed, including source code, name, and front page file name.

        Args:
            source (object): Used to store the source code of a web page.
            name (str): Used to set the name of the web page being initialized.
            filename_case (str): Used to set the name of the file containing the
                HTML front page, with the option to restore the original framing
                of the file's path.
            restore_framing (bool): Used to control whether the framing of the
                HTML file should be restored after it has been modified.
            frontpage (ospathbasename): Set to the value of "index.html" by default.

        """
        self.source = source
        # object inside CHM file
        self.name = name
        self.filename_case = filename_case
        self.restore_framing = restore_framing
        # frontpage name to substitute
        self.frontpage = os.path.basename(frontpage)

    def read(self):
        return self.source.get(self.name)

    def lower_links(self, text):
        """
        Replaces all occurrences of URLs with lowercase letters in the given text.

        Args:
            text (str): Passed to the regular expression sub method to be modified
                by replacing certain attributes with lowercase versions.

        Returns:
            str: The result of applying a regular expression substitution to the
            given input `text`.

        """
        return re.sub(
            b"(?i)(href|src)\\s*=\\s*([^\\s|>]+)",
            lambda m: m.group(0).lower(),
            text,
        )

    def add_restoreframing_js(self, name, text):
        """
        Modifies JavaScript code to add a link for framing to the page's HTML body
        tag, based on the page's name and depth of nested frames.

        Args:
            name (str): Used to generate a JavaScript code for restoring framing
                based on the provided name.
            text (encoded): 14 characters long.

        Returns:
            str: A modified version of the given `text` string with the inclusion
            of a script tag that links to the framing page.

        """
        name = re.sub("/+", "/", name)
        depth = name.count("/")

        js = b"""<body><script language="javascript">
if (window.name != "content")
    document.write("<center><a href='%s%s?page=%s'>show framing</a></center>")
</script>""" % (
            b"../" * depth,
            self.frontpage.encode("utf8"),
            name.encode("utf8"),
        )

        return re.sub(b"(?i)<\\s*body\\s*>", js, text)

    def correct(self):
        """
        Reads data from an instance of the `self` class, modifies it by removing
        certain tags and attributes, and returns the modified data.

        Returns:
            str: A modified version of the input data, where certain HTML tags and
            patterns have been removed or replaced.

        """
        data = self.read()
        # If entry is a html page?
        if re.search("(?i)\\.html?$", self.name) and data is not None:
            # lower-casing links if needed
            if self.filename_case:
                data = self.lower_links(data)

            # Delete unwanted HTML elements.
            data = re.sub(b"<div .*teamlib\\.gif.*\\/div>", b"", data)
            data = re.sub(b"<a href.*>\\[ Team LiB \\]<\\/a>", b"", data)
            data = re.sub(
                b"<table.*larrow\\.gif.*rarrow\\.gif.*<\\/table>", b"", data
            )
            data = re.sub(b"<a href.*next\\.gif[^>]*><\\/a>", b"", data)
            data = re.sub(b"<a href.*previous\\.gif[^>]*><\\/a>", b"", data)
            data = re.sub(b"<a href.*prev\\.gif[^>]*><\\/a>", b"", data)
            data = re.sub(b'"[^"]*previous\\.gif"', b'""', data)
            data = re.sub(b'"[^"]*prev\\.gif"', b'""', data)
            data = re.sub(b'"[^"]*next\\.gif"', b'""', data)
        if data is not None:
            return data
        else:
            return b""

    def get(self):
        """
        In the Entry class reads entry content, performs various modifications
        based on configuration options, and returns the modified content or the
        default value '````.

        Returns:
            str: The content of an entry.

        """
        # read entry content
        data = self.read()
        # If entry is a html page?
        if re.search("(?i)\\.html?$", self.name) and data is not None:
            # lower-casing links if needed
            if self.filename_case:
                data = self.lower_links(data)
            # restore framing if that option is set in config file
            if self.restore_framing:
                data = self.add_restoreframing_js(self.name[1:], data)
        if data is not None:
            return data
        else:
            return b""
