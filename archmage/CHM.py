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
    Defines five constants representing different actions for working with HTML
    files, including extracting content, dumping HTML, converting from CHM to TXT
    or HTML, and converting from CHM to PDF.

    Attributes:
        EXTRACT (int): 1, indicating that the action extracts information from a
            source file.
        DUMPHTML (int): 2 in value, indicating that it extracts HTML content from
            a CHM file.
        CHM2TXT (str): 3 in value, indicating that it converts CHM files to plain
            text format.
        CHM2HTML (int): 4th in the enumeration, representing the action of converting
            Chemistry Markup Language (ChemML) files to HTML format.
        CHM2PDF (int): 5, which represents the action of converting CHM files to
            PDF format.

    """
    EXTRACT = 1
    DUMPHTML = 2
    CHM2TXT = 3
    CHM2HTML = 4
    CHM2PDF = 5


PARENT_RE = re.compile(r"(^|/|\\)\.\.(/|\\|$)")


class FileSource:
    """
    Provides methods to list and retrieve files from a CHM file.

    Attributes:
        _chm (chmlibchm_t): A handle to a CHM file, which allows for reading and
            manipulation of its contents.

    """
    def __init__(self, filename):
        self._chm = chmlib.chm_open(filename)

    def listdir(self):
        """
        Iterates over all files in a ChM (Chemical Markup) file and appends their
        paths to a list.

        Returns:
            List[str]: A list of strings containing the file paths in the specified
            directory.

        """
        def get_name(chmfile, ui, out):
            """
            Retrieves the path to a Chemical Name file based on user input and
            appends it to a list before returning the CHM enumerator's continuation
            value.

            Args:
                chmfile (File): Passed as an argument to the function, indicating
                    the file from which the name will be extracted.
                ui (chmlib.UI): Used to store a path that is passed from the user
                    interface.
                out (List[str]): Used to store the path of files that are returned
                    from the function.

            Returns:
                chmlibCHM_ENUMERATOR_CONTINUE|str: A continue flag indicating
                whether to continue processing the next item in the CHM file or not.

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
        Resolves an object in the CHM file using the `chmlib.chm_resolve_object()`
        function, retrieves the contents of the resolved object using the
        `chmlib.chm_retrieve_object()` function, and returns the contents as a string.

        Args:
            name (str): Used to retrieve an object from the CHM file.

        Returns:
            bytes: A slice of memory containing the contents of an object in the
            CHM file.

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
    Provides a directory-based file system interface, offering methods for listing
    files and reading their contents.

    Attributes:
        dirname (str): Initialized to the directory path provided during initialization,
            representing the root directory for which file listings are retrieved
            and files are read.

    """
    def __init__(self, dirname):
        self.dirname = dirname
    def listdir(self):
        """
        Iterates through all subdirectories and files within a specified directory,
        and returns a list of relative paths to each file.

        Returns:
            List[str]: A list of file paths relative to the directory specified
            in the class instance attribute `dirname`.

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
        Retrieves a file from the directory and reads it into memory as a bytes
        object, returning the contents of the file if successful, or `None` otherwise.

        Args:
            filename (str): Used to specify the file name to be read from the directory.

        Returns:
            bytes: The contents of a file located in the directory specified by
            `self.dirname and filename`.

        """
        with open(self.dirname + filename, "rb") as fh:
            if fh is None:
                return None
            return fh.read()
    def close(self):
        pass


class CHM:
    """
    Represents a CHM (HyperText Markup Language) file, providing methods to extract
    entries, templates, and images from the file and save them in a specified
    directory. It also offers the ability to process templates, extract entries,
    and create an HTML document from the extracted content.

    Attributes:
        cache (Dict[str,Any]): Used to store intermediate results of the CHM file
            generator, such as the list of HTML files, the list of image URLs, and
            other metadata.
        source (Union[DirSource,FileSource]): Used to represent the source of the
            CHM file, which can be either a directory or a file.
        sourcename (str|str): Used to store the source name of a CHM file.
        __dict__ (Dict[str,Any]): Used to store the instance variables of the
            class. It contains the values of the attributes and methods of the
            class, which can be accessed using their attribute names.
        aux_re (RegexMatcher|str): Used to filter out non-HTML entries from the
            CHM file's contents.
        auxes (Union[str,List[str]]): Used to store a list of strings that represent
            regular expressions for auxilary file names in the CHM file.
        topicstree (Union[DirSource,FileSource]): Used to store the source of the
            topic tree.
        topics (List[str]): Used to store a list of topics for which the CHM file
            contains content.
        contents (Dict[str,str]): A container for the contents of the CHM file,
            which can include HTML files, image files, and other data.

    """
    def __init__(self, name):
        """
        Initializes instance variables and performs various actions, including:
        * Setting cache and source directories
        * Reading and executing configuration file contents
        * Creating an auxillary regular expression for topic searching
        * Parsing a topics tree and setting `topicstree` attribute.

        Args:
            name (Union[str, Path]): Used to specify the source file or directory
                for which the Sitemap class will generate content.

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
        Retrieves or caches a list of entries associated with the class instance,
        upon the first call it generates the list internally and after that it
        returns the cached value.

        Returns:
            list[str]: A cache of the method `_entries()` calls.

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
        Retrieves and caches a list of HTML files in the directory where the CHM
        class was executed, if not already cached.

        Returns:
            list[str]: A cache of the list of files with the extension .html in
            the current directory and its subdirectories, generated by calling the
            internal function `_html_files()`.

        """
        if "html_files" not in self.cache:
            self.cache["html_files"] = self._html_files()
        return self.cache["html_files"]

    def _html_files(self):
        """
        Generates a list of HTML files in a given directory and its subdirectories
        by feeding the topic tree with the `PageLister` object.

        """
        lister = PageLister()
        lister.feed(self.topicstree)
        return lister.pages

    # retrieves the list of images urls contained into the CHM file.
    # (actually performed by the ImageCatcher class)
    def image_urls(self):
        """
        Retrieves and caches image URLs based on a private `_image_urls` method call.

        Returns:
            Dict[str,str]: A cache of image URLs.

        """
        if "image_urls" not in self.cache:
            self.cache["image_urls"] = self._image_urls()
        return self.cache["image_urls"]

    def _image_urls(self):
        """
        Generates a list of image URLs present in an HTML file by using the
        `ImageCatcher` class to catch and decode images.

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
        Retrieves or caches the list of image files associated with the object.

        Returns:
            List[str]: A list of image files contained within the current working
            directory.

        """
        if "image_files" not in self.cache:
            self.cache["image_files"] = self._image_files()
        return self.cache["image_files"]

    def _image_files(self):
        """
        Updates an internal dictionary `out` with image URLs and their corresponding
        entry names from `self.image_urls()` and `self.entries()`, respectively,
        while ignoring already existing entries in `out`.

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
        Retrieves or caches the topics list if it's not already available, returning
        the cached list.

        Returns:
            list[str]: A cache of the topics.

        """
        if "topics" not in self.cache:
            self.cache["topics"] = self._topics()
        return self.cache["topics"]

    def _topics(self):
        """
        Recursively traverses through the file's entries and returns an Entry
        object containing the topics found in the file.

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
        Sets or retrieves a topic value from a cache based on the key "deftopic".

        Returns:
            object: A result of calling the `_deftopic` internal method.

        """
        if "deftopic" not in self.cache:
            self.cache["deftopic"] = self._deftopic()
        return self.cache["deftopic"]

    def _deftopic(self):
        """
        Modifies the file path of an HTML file by removing any leading slashes and
        then lowercasing the resulting string.

        """
        if self.html_files()[0].startswith("/"):
            return self.html_files()[0].replace("/", "", 1).lower()
        return self.html_files()[0].lower()

    # Get frontpage name
    def frontpage(self):
        """
        Checks if the "frontpage" key exists in its cache, otherwise it calls the
        `_frontpage` method and stores the result in the cache.

        Returns:
            object: Determined by calling the internal method `_frontpage()` and
            storing it in a cache.

        """
        if "frontpage" not in self.cache:
            self.cache["frontpage"] = self._frontpage()
        return self.cache["frontpage"]

    def _frontpage(self):
        """
        Determines the URL of the front page (/) by iterating through a list of
        files and selecting the first one with the filename "index.html". If the
        front page is found, it renames the file to "index<index>.html" and
        increments the index value. The final frontpage URL is returned.

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
        Retrieves and caches a list of templates if not already cached, and returns
        the cached list.

        Returns:
            list[str]: A cache of predefined templates for the current user.

        """
        if "templates" not in self.cache:
            self.cache["templates"] = self._templates()
        return self.cache["templates"]

    def _templates(self):
        """
        Lists all templates in a directory and filters them based on whether they
        are already included in the project's entries. The filtered list is returned
        as a list of file paths.

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
        Retrieves or calculates and stores the toclevels attribute value in the
        object's cache, which is then returned upon subsequent calls.

        Returns:
            list[str]: A cached result of calling the `_toclevels()` method.

        """
        if "toclevels" not in self.cache:
            self.cache["toclevels"] = self._toclevels()
        return self.cache["toclevels"]

    def _toclevels(self):
        """
        Determines the maximum level of toc recursively by feeding the topic tree
        with the decode "latin-1" and counting the number of topics exceeding the
        specified limit.

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
        Retrieves an HTML template based on the input `name`, opening and reading
        the relevant template file and passing parameters to a Template instance
        for substitution.

        Args:
            name (str): Used to specify the template to be rendered.

        Returns:
            str: A modified version of an HTML template file based on the input
            parameter `name`.

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
        Writes HTML templates to a specified destination directory and copies icons
        to a subdirectory within the destined directory.

        Args:
            destdir (str | Path): Used to specify the directory where the generated
                HTML files will be written.

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
        Extracts an entry from a source file and saves it to a specified output
        file, creating directories as needed.

        Args:
            entry (Entry | str): Used to specify the entry to be extracted from
                the source code.
            output_file (str | Path): Used to specify the output file path for the
                extracted entry.
            destdir (str | List[str]): Used to specify the directory where the
                extracted entry will be saved. It can either be a single directory
                path or a list of directories, separated by ",".
            correct (bool): Used to indicate whether the output file should be
                created with correct or incorrect framing.

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
        Extracts entries from a list and performs actions based on the entry's
        format and contents, including checking for malicious names and raising
        errors if necessary.

        Args:
            entries (List[str]): Used to store the entries to be extracted from
                the input file.
            destdir (str): Used to specify the destination directory for extracted
                entries.
            correct (bool): Used to indicate whether the entry should be extracted
                or not.

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
        Performs the following tasks:
        * Creates the destination directory if it does not exist.
        * Extracts entries from the CHM file to the destination directory.
        * Processes templates in the destination directory.

        Args:
            destdir (str): Used to specify the directory where the extracted files
                will be saved.

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
        Iterates through a list of HTML files and prints out the content of each
        file, filtered by a regular expression pattern to exclude certain files.

        Args:
            output (FileIO | str): Used to write the output of the function to a
                file or the console.

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
        Processes HTML files and converts them into plain text format using the
        `chmtotext` command.

        Args:
            output (Optional[io.Text]): Used to specify the destination for the
                generated text.

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
        Extracts CHM content, creates a temporary directory, and converts it to
        HTML or PDF format using the specified options and executes the resulting
        HTML document.

        Args:
            output (Union[str, Path]): Used to specify the output file path for
                the generated HTML document.
            format (Action.CHM2HTML | Action.CHM2PDF): Used to specify the output
                format of the document, either CHM2HTML or CHM2PDF.

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
    Manages entry content, including reading and manipulating links, adding framing
    JavaScript, and correcting certain issues with HTML entities and filenames.

    Attributes:
        source (object): Used to store the entry's source content.
        name (str): Used to store the name of the entry.
        filename_case (str): Used to specify whether the entry filename should be
            lowercased when searching for links within its content.
        restore_framing (bool): Used to enable or disable restoring framing for
            links within the entry content.
        frontpage (str): Used to specify the name of the front page file for framing
            links.

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
        Initializes instance variables source, name, filename_case, restore_framing,
        and frontpage.

        Args:
            source (str): Assigned to the attribute of the same name, storing the
                initial value of the object.
            name (str): Used to assign a name to the framing element.
            filename_case (str): Used to set the case of the filename when restoring
                framing.
            restore_framing (bool): Used to restore the original framing of the
                HTML document when it was parsed, providing more accurate results
                for some parsers.
            frontpage (str | str): Used to specify the name of the HTML file that
                serves as the front page of the website, with the default value
                being "index.html".

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
        Replaces all occurrences of href or src attributes in a given string with
        their lowercase versions, preserving the rest of the string unchanged.

        Args:
            text (str): The string to be processed with regular expression
                substitution for lowercasing hyperlink attributes.

        Returns:
            str: The result of replacing all occurrence of href or src with their
            corresponding lowercase version inside a given string using a lambda
            function.

        """
        return re.sub(
            b"(?i)(href|src)\\s*=\\s*([^\\s|>]+)",
            lambda m: m.group(0).lower(),
            text,
        )

    def add_restoreframing_js(self, name, text):
        """
        Modifies the provided string `text` by adding JavaScript code that displays
        a link to the framing page for the current entry when the page is loaded.

        Args:
            name (str): Rewritten to exclude any forward slashes and then passed
                through a depth counter to generate the script for framing restoration.
            text (str): Passed as an argument to re.sub method.

        Returns:
            str: A modified version of the original text, where certain parts have
            been replaced with JavaScript code for framing functionality.

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
        Modifies an input string based on its name and filename case, replacing
        certain HTML tags and links related to Team Lib.

        Returns:
            str: The modified string after applying the given regular expressions
            to remove unwanted elements.

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
        Reads the entry content, modifies it based on various options, and returns
        the modified content or None if no modification is needed.

        Returns:
            str: Either the contents of the entry or a modified version of it
            depending on various conditions and configuration options.

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
