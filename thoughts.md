I need to build an application that allows me to lookup specifications of bicycle components from PDF files. An example file is ./pdf.pdf  The goal is to make it easier for mechanics in a bicycle shop to work with the parts.  The lookups need to be able to be done with natural language. Results have to include a screenshot of the area of the PDF from which the answer came as well as a link to that PDF. A link to the specific page of the pdf is preferred but not required.  This should be an API and can use Anthropic models where needed. Any additional software such as vector databases can be acquired for this project. You are required to present me with at least 3 alternatives for each piece of additional software and interview me concerning the pro's and con's of each before we decide on one.  I will build a front-end that calls the API later.

I have used Python in the past and need to refamiliarize myself with it. Your goal is to output a set of skills and agents that will help me to build this the "old fashioned way".  I need to physically type and understand the lines of code and will use the skills and agents for questions when I have them. To start I will need hints as to the technology stack to use (partly determined by interviews we will do together) and a very high level description of the codebase along with explanations of why certain decisions have been made.  As much as is realistic this should follow domain and other best practices that one would expect to find in a piece of software from a Fortune 100 company. 

## Examples

These are examples from the file ./pdf.pdf page numbers are page numbers as printed in the lower right-hand corner of each page of the PDF. The values here are the size of tool required followed by the torque that should be applied by the mechanic using each tool. 

Page 27 : 7 - 8 mm
Page 28 : 40 N-m (354 in-lb)
Page 31 : 5mm hex key 11 N-m (97 in-lb)
Page 50 : T25 3 N-m (27 in-lb)
Page 50 : T25 2 N-m (18 in-lb)
Page 51 : 4mm hex key T25 5.5 N-m (49 in-lb)
Page 51 : T25 3 N-m (27 in-lb)
Page 51 : 2.5mm hex key 2 N-m (18 in-lb)
Page 51 : T25 3 N-m (27 in-lb)
