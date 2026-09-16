# Decisions

1. Add another ENUM type for INCORRECT_DATATYPE

2. Hatchling is used for the backend 

3. To make it vendor agnostic file names are parsed differently the assumption here is that image names are like this

a2_front/a2_front_rgb.jpg, that implies that naming convention is as follows <checkpointname><image type (rgb or thermal)> 
ac_2_h000_thermal.jpg implies that naming convention is <checkpointname><direction or other name><image type (rgb or thermal)>

so splitting is done on this rule on that we have checkpointname,direction,thermal or rgb

4. I have added a style config. Where we can select the style to be used, this can be helpful for different vendors where if you need a specific style for a vendor you can just tweak the config file and select the required style

5. For the acceptance I have changed the run.json accordingly to mimic the test cases. If you want you can look into them and run them via commands in README.md

6. The code is modular but not optmized , if needed later that can be done

