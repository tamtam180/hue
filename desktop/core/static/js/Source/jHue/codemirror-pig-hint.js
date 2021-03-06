// Licensed to Cloudera, Inc. under one
// or more contributor license agreements.  See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership.  Cloudera, Inc. licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License.  You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

(function () {
  function forEach(arr, f) {
    for (var i = 0, e = arr.length; i < e; ++i) f(arr[i]);
  }

  function arrayContains(arr, item) {
    if (!Array.prototype.indexOf) {
      var i = arr.length;
      while (i--) {
        if (arr[i] === item) {
          return true;
        }
      }
      return false;
    }
    return arr.indexOf(item) != -1;
  }

  function scriptHint(editor, _keywords, getToken) {
    // Find the token at the cursor
    var cur = editor.getCursor(), token = getToken(editor, cur), tprop = token;
    // If it's not a 'word-style' token, ignore the token.

    if (token.string.indexOf("'") == 0){
      CodeMirror.isPath = true;
      token.string = token.string.substring(1, token.string.length);
    }
    if (token.string.indexOf("/") > -1){
      token.string = token.string.substring(token.string.lastIndexOf("/") + 1);
    }

    if (!/^[\w$_]*$/.test(token.string)) {
      token = tprop = {start: cur.ch, end: cur.ch, string: "", state: token.state,
        className: token.string == ":" ? "pig-type" : null};
    }

    if (!context) var context = [];
    context.push(tprop);

    var completionList = getCompletions(token, context);
    //prevent autocomplete for last word, instead show dropdown with one word
    if (completionList.length == 1) {
      completionList.push(" ");
    }

    return {list: completionList,
      from: CodeMirror.Pos(cur.line, token.start),
      to: CodeMirror.Pos(cur.line, token.end)};
  }

  CodeMirror.isPath = false;
  CodeMirror.currentFiles = [];

  CodeMirror.pigHint = function (editor) {
    return scriptHint(editor, pigKeywordsU, function (e, cur) {
      return e.getTokenAt(cur);
    });
  };

  var pigKeywords = "VOID IMPORT RETURNS DEFINE LOAD FILTER FOREACH ORDER CUBE DISTINCT COGROUP "
      + "JOIN CROSS UNION SPLIT INTO IF OTHERWISE ALL AS BY USING INNER OUTER ONSCHEMA PARALLEL "
      + "PARTITION GROUP AND OR NOT GENERATE FLATTEN ASC DESC IS STREAM THROUGH STORE MAPREDUCE "
      + "SHIP CACHE INPUT OUTPUT STDERROR STDIN STDOUT LIMIT SAMPLE LEFT RIGHT FULL EQ GT LT GTE LTE "
      + "NEQ MATCHES TRUE FALSE DUMP";
  var pigKeywordsU = pigKeywords.split(" ");
  var pigKeywordsL = pigKeywords.toLowerCase().split(" ");

  var pigTypes = "BOOLEAN INT LONG FLOAT DOUBLE CHARARRAY BYTEARRAY BAG TUPLE MAP";
  var pigTypesU = pigTypes.split(" ");
  var pigTypesL = pigTypes.toLowerCase().split(" ");

  var pigBuiltins = "ABS ACOS ARITY ASIN ATAN AVG BAGSIZE BINSTORAGE BLOOM BUILDBLOOM CBRT CEIL "
      + "CONCAT COR COS COSH COUNT COUNT_STAR COV CONSTANTSIZE CUBEDIMENSIONS DIFF DISTINCT DOUBLEABS "
      + "DOUBLEAVG DOUBLEBASE DOUBLEMAX DOUBLEMIN DOUBLEROUND DOUBLESUM EXP FLOOR FLOATABS FLOATAVG "
      + "FLOATMAX FLOATMIN FLOATROUND FLOATSUM GENERICINVOKER INDEXOF INTABS INTAVG INTMAX INTMIN "
      + "INTSUM INVOKEFORDOUBLE INVOKEFORFLOAT INVOKEFORINT INVOKEFORLONG INVOKEFORSTRING INVOKER "
      + "ISEMPTY JSONLOADER JSONMETADATA JSONSTORAGE LAST_INDEX_OF LCFIRST LOG LOG10 LOWER LONGABS "
      + "LONGAVG LONGMAX LONGMIN LONGSUM MAX MIN MAPSIZE MONITOREDUDF NONDETERMINISTIC OUTPUTSCHEMA  "
      + "PIGSTORAGE PIGSTREAMING RANDOM REGEX_EXTRACT REGEX_EXTRACT_ALL REPLACE ROUND SIN SINH SIZE "
      + "SQRT STRSPLIT SUBSTRING SUM STRINGCONCAT STRINGMAX STRINGMIN STRINGSIZE TAN TANH TOBAG "
      + "TOKENIZE TOMAP TOP TOTUPLE TRIM TEXTLOADER TUPLESIZE UCFIRST UPPER UTF8STORAGECONVERTER";
  var pigBuiltinsU = pigBuiltins.split(" ").join("() ").split(" ");
  var pigBuiltinsL = pigBuiltins.toLowerCase().split(" ").join("() ").split(" ");
  var pigBuiltinsC = ("BagSize BinStorage Bloom BuildBloom ConstantSize CubeDimensions DoubleAbs "
      + "DoubleAvg DoubleBase DoubleMax DoubleMin DoubleRound DoubleSum FloatAbs FloatAvg FloatMax "
      + "FloatMin FloatRound FloatSum GenericInvoker IntAbs IntAvg IntMax IntMin IntSum "
      + "InvokeForDouble InvokeForFloat InvokeForInt InvokeForLong InvokeForString Invoker "
      + "IsEmpty JsonLoader JsonMetadata JsonStorage LongAbs LongAvg LongMax LongMin LongSum MapSize "
      + "MonitoredUDF Nondeterministic OutputSchema PigStorage PigStreaming StringConcat StringMax "
      + "StringMin StringSize TextLoader TupleSize Utf8StorageConverter").split(" ").join("() ").split(" ");

  function getCompletions(token, context) {
    var found = [], start = token.string, extraFound = [];

    function maybeAdd(str) {
      var stripped = strip(str).replace(/(?:(?:^|\n)\s+|\s+(?:$|\n))/g,'').replace(/\s+/g,' ');
      if (stripped.indexOf(start) == 0 && !arrayContains(found, str)) found.push(str);
    }

    function maybeAddToExtra(str) {
      if (str.indexOf(start) == 0 && !arrayContains(found, str)) extraFound.push(str);
    }

    function strip(html){
      var tmp = document.createElement("DIV");
      tmp.innerHTML = html;
      return tmp.textContent||tmp.innerText;
    }

    function gatherCompletions(obj) {
      if (CodeMirror.isPath || obj.indexOf("'") == 0){
        forEach(CodeMirror.currentFiles, maybeAdd);
      }
      else {
        if (obj == ":") {
          forEach(pigTypesL, maybeAdd);
        }
        else {
          forEach(pigBuiltinsU, maybeAdd);
          forEach(pigBuiltinsL, maybeAdd);
          forEach(pigBuiltinsC, maybeAdd);
          forEach(pigTypesU, maybeAdd);
          forEach(pigTypesL, maybeAdd);
          forEach(pigKeywordsU, maybeAdd);
          forEach(pigKeywordsL, maybeAdd);
          forEach(CodeMirror.availableVariables, maybeAddToExtra);
        }
      }
    }

    if (context) {
      // If this is a property, see if it belongs to some object we can
      // find in the current environment.
      var obj = context.pop(), base;
      base = obj.string;

      while (base != null && context.length)
        base = base[context.pop().string];
      if (base != null) gatherCompletions(base);
    }
    return extraFound.sort().concat(found.sort());
  }
})();
