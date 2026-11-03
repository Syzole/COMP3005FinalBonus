As a note all the traces in the 

# Capcity math
I used the following formula:
For fixed length records of L bytes, the number per page is floor(4088 / (L + 4)). 

So for a schme with 3 ints, that is 4*3 => L = 12

So 4088 / 16 => 255.5 => 255 records per page

For a scehma of a INT, b VARCHAR(5)='bob', c INT, L = (4 + (2+3) + 4) => 13

So 4088/17 = 240.47 => 240 records per page

For a schema that has a (a INT, b VARCHAR(200)) => L = (4 + (2+100)) = 106, for the note we picked ours string to be x 100 times

So 4088/110 = 37.16 => 37 records per page