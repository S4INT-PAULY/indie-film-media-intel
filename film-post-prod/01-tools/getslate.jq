def units:
  {
    zero: 0,
    one: 1,
    two: 2,
    three: 3,
    four: 4,
    five: 5,
    six: 6,
    seven: 7,
    eight: 8,
    nine: 9
  };

def teens:
  {
    ten: 10,
    eleven: 11,
    twelve: 12,
    thirteen: 13,
    fourteen: 14,
    fifteen: 15,
    sixteen: 16,
    seventeen: 17,
    eighteen: 18,
    nineteen: 19
  };

def tens:
  {
    twenty: 20,
    thirty: 30,
    forty: 40,
    fifty: 50,
    sixty: 60,
    seventy: 70,
    eighty: 80,
    ninety: 90
  };

def spoken_number:
  ascii_downcase
  | gsub("-"; " ")
  | split(" ")
  | if length == 1 then
      if units[.[0]] != null then units[.[0]]
      elif teens[.[0]] != null then teens[.[0]]
      elif tens[.[0]] != null then tens[.[0]]
      else null
      end
    elif length == 2 and tens[.[0]] != null and units[.[1]] != null then
      tens[.[0]] + units[.[1]]
    else
      null
    end;

def extract_numbers:
  [
    scan(
      "(?i)\\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?\\b|[-+]?\\d+(?:\\.\\d+)?"
    )
  ]
  | map(
      if test("^[+-]?\\d") then .
      else spoken_number | tostring
      end
    );

.assets[]
| {
    filename,
    slateCall: (
      (.transcript // "")
      | (match("(?s)^(.*?Action)"; "g") | .string) // empty
    ),
    numbers: (
      (.transcript // "")
      | extract_numbers
    )
  }
| {
    filename,
    slateCall,
    scene: (.numbers[0] // null),
    take: (
      if (.numbers | length) > 2 and .numbers[1] == .numbers[0]
      then .numbers[2]
      else .numbers[1] // null
      end
    )
  }